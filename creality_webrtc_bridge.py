import asyncio
import base64
import io
import json
import logging
import os
import re
import sys
import time
from typing import Optional, Set
import aiohttp
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from PIL import Image

# Force unbuffered stdout logging
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# Config file location when running as a Home Assistant Add-on
HA_OPTIONS_PATH = "/data/options.json"

# Fallback / Default settings
DEFAULT_CONFIG = {
    "printer_ip": "192.168.10.161",
    "printer_port": 8000,
    "stream_port": 8080,
    "target_fps": 15,
    "jpeg_quality": 80,
    "log_level": "info",
}

def load_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(HA_OPTIONS_PATH):
        try:
            with open(HA_OPTIONS_PATH, "r", encoding="utf-8") as f:
                user_options = json.load(f)
                config.update(user_options)
                print(f"[INIT] Laddade konfiguration från {HA_OPTIONS_PATH}: {user_options}", flush=True)
        except Exception as err:
            print(f"[WARN] Kunde inte läsa {HA_OPTIONS_PATH} ({err}). Använder standardvärden.", flush=True)
    else:
        # Check environment variables
        if os.environ.get("PRINTER_IP"):
            config["printer_ip"] = os.environ.get("PRINTER_IP")
        if os.environ.get("PRINTER_PORT"):
            config["printer_port"] = int(os.environ.get("PRINTER_PORT"))
        if os.environ.get("STREAM_PORT"):
            config["stream_port"] = int(os.environ.get("STREAM_PORT"))
        if os.environ.get("TARGET_FPS"):
            config["target_fps"] = int(os.environ.get("TARGET_FPS"))
        if os.environ.get("JPEG_QUALITY"):
            config["jpeg_quality"] = int(os.environ.get("JPEG_QUALITY"))
        if os.environ.get("LOG_LEVEL"):
            config["log_level"] = os.environ.get("LOG_LEVEL")

    return config

config = load_config()

log_level_map = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
logging.basicConfig(
    level=log_level_map.get(config.get("log_level", "info").lower(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("creality_webrtc")

def sanitize_sdp(sdp: str) -> str:
    """Sanerar SDP-svaret från skrivaren så att aiortc godkänner H264 parametrar."""
    logger.info("--- URSPRUNGLIG SDP ANSWER FRÅN SKRIVAREN ---\n%s", sdp)
    lines = sdp.splitlines()
    new_lines = []

    # Hitta alla payload types för H264 i rtpmap
    h264_pts = set()
    for line in lines:
        m = re.match(r'^a=rtpmap:(\d+)\s+H264/90000', line, re.IGNORECASE)
        if m:
            h264_pts.add(m.group(1))

    existing_fmtp_pts = set()
    for line in lines:
        m = re.match(r'^a=fmtp:(\d+)', line)
        if m:
            existing_fmtp_pts.add(m.group(1))

    for line in lines:
        # Om skrivaren svarar med a=setup:passive, tvinga a=setup:active i svaret så att aiortc/skrivaren genomför DTLS korrekt
        if line.strip() == "a=setup:passive":
            new_lines.append("a=setup:active")
            continue

        fmtp_match = re.match(r'^a=fmtp:(\d+)', line)
        if fmtp_match and fmtp_match.group(1) in h264_pts:
            pt = fmtp_match.group(1)
            # Tvinga aiortc-kompatibla fmtp-parametrar
            new_lines.append(f"a=fmtp:{pt} level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f")
            continue

        rtpmap_match = re.match(r'^a=rtpmap:(\d+)\s+H264/90000', line, re.IGNORECASE)
        if rtpmap_match:
            pt = rtpmap_match.group(1)
            new_lines.append(line)
            if pt not in existing_fmtp_pts:
                # Om a=fmtp saknas för H264, lägg till den direkt under rtpmap
                new_lines.append(f"a=fmtp:{pt} level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f")
            continue

        new_lines.append(line)

    sanitized = "\r\n".join(new_lines) + "\r\n"
    logger.info("--- SANERAD SDP ANSWER FÖR AIORTC ---\n%s", sanitized)
    return sanitized

class CameraBridge:
    def __init__(self, cfg: dict):
        self.printer_ip = cfg["printer_ip"]
        self.printer_port = cfg["printer_port"]
        self.stream_port = cfg["stream_port"]
        self.target_fps = cfg["target_fps"]
        self.jpeg_quality = cfg["jpeg_quality"]

        self.webrtc_url = f"http://{self.printer_ip}:{self.printer_port}/call/webrtc_local"
        self.latest_frame: Optional[bytes] = None
        self.frame_event = asyncio.Event()
        self.connected = False
        self.last_frame_time = 0.0
        self.fps_interval = 1.0 / self.target_fps if self.target_fps > 0 else 0.0
        self.pc: Optional[RTCPeerConnection] = None
        self.received_frames_count = 0

    async def connect_webrtc(self):
        """Initierar WebRTC handskakning med skrivaren och hanterar mottagna bildrutor."""
        while True:
            try:
                logger.info("Ansluter till Creality K1 WebRTC på %s...", self.webrtc_url)
                rtc_cfg = RTCConfiguration(
                    iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]
                )
                self.pc = RTCPeerConnection(configuration=rtc_cfg)
                self.pc.addTransceiver("video", direction="recvonly")

                track_received = asyncio.Event()

                @self.pc.on("track")
                def on_track(track):
                    logger.info("Mottog track event! Track kind: %s, ID: %s", track.kind, track.id)
                    if track.kind == "video":
                        logger.info("Mottog videotrack! Börjar avkoda videoström...")
                        track_received.set()
                        asyncio.create_task(self._process_video_track(track))

                @self.pc.on("connectionstatechange")
                async def on_connectionstatechange():
                    logger.info("WebRTC connectionState: %s", self.pc.connectionState)
                    if self.pc.connectionState in ["failed", "closed", "disconnected"]:
                        self.connected = False

                @self.pc.on("iceconnectionstatechange")
                async def on_iceconnectionstatechange():
                    logger.info("WebRTC iceConnectionState: %s", self.pc.iceConnectionState)

                # 1. Skapa lokal SDP offer
                offer = await self.pc.createOffer()
                await self.pc.setLocalDescription(offer)

                # Vänta på att ICE-gathering slutförs så alla lokala kandidater (IPs) inkluderas i offer!
                if self.pc.iceGatheringState != "complete":
                    logger.info("Väntar på att lokala ICE-kandidater samlas in...")
                    gather_evt = asyncio.Event()
                    @self.pc.on("icegatheringstatechange")
                    def on_ice_state():
                        if self.pc.iceGatheringState == "complete":
                            gather_evt.set()
                    try:
                        await asyncio.wait_for(gather_evt.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        logger.info("ICE gathering avslutades efter timeout, fortsätter...")

                offer_sdp = self.pc.localDescription.sdp
                # Anpassa a=setup i offer till active för att tvinga DTLS-handskakningen att genomföras utan timeout
                offer_sdp_active = offer_sdp.replace("a=setup:actpass", "a=setup:active")
                logger.info("Lokal SDP Offer som skickas till skrivaren:\n%s", offer_sdp_active)

                # 2. Paketera i JSON & Base64-koda
                payload_json = {
                    "type": "offer",
                    "sdp": offer_sdp_active
                }
                b64_payload = base64.b64encode(json.dumps(payload_json).encode("utf-8")).decode("utf-8")

                # 3. Skicka till skrivaren via HTTP POST
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    headers = {"Content-Type": "text/plain"}
                    async with session.post(self.webrtc_url, data=b64_payload, headers=headers) as resp:
                        if resp.status == 200:
                            raw_answer = await resp.text()
                            decoded_json = json.loads(base64.b64decode(raw_answer).decode("utf-8"))
                            
                            original_sdp = decoded_json["sdp"]

                            # Sanera SDP för kompabilitet med aiortc
                            sanitized_sdp = sanitize_sdp(original_sdp)

                            answer = RTCSessionDescription(sdp=sanitized_sdp, type=decoded_json["type"])
                            await self.pc.setRemoteDescription(answer)
                            logger.info("WebRTC-handskakning genomförd med skrivaren!")
                            self.connected = True
                        else:
                            logger.error("Fel vid WebRTC-anrop till skrivaren (HTTP %s)", resp.status)
                            self.connected = False
                            await self.pc.close()
                            await asyncio.sleep(5)
                            continue

                # Vänta på att spåret startar eller anslutningen misslyckas
                try:
                    await asyncio.wait_for(track_received.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    logger.warning("Timeout (15s) i väntan på videotrack-signal från skrivaren.")

                # Håll loopen igång så länge anslutningen är aktiv
                while self.connected and self.pc and self.pc.connectionState not in ["failed", "closed"]:
                    await asyncio.sleep(2)

            except asyncio.CancelledError:
                logger.info("Avbryter WebRTC-anslutning...")
                break
            except Exception as err:
                logger.error("Ett fel uppstod i WebRTC-loopen: %s", err, exc_info=True)

            self.connected = False
            if self.pc:
                await self.pc.close()
                self.pc = None

            logger.info("Återansluter om 5 sekunder...")
            await asyncio.sleep(5)

    async def _process_video_track(self, track):
        """Avkodar videoframes från WebRTC-spåret och konverterar till JPEG."""
        logger.info("Börjar ta emot bildrutor från videospåret...")
        while self.connected:
            try:
                frame = await track.recv()
                self.received_frames_count += 1
                if self.received_frames_count == 1:
                    logger.info("Mottog FÖRSTA bildrutan från skrivarkameran! (Upplösning: %sx%s)", frame.width, frame.height)

                now = time.time()
                if self.fps_interval > 0 and (now - self.last_frame_time) < self.fps_interval:
                    continue
                self.last_frame_time = now

                # Konvertera PyAV frame till PIL Image och spara som JPEG
                img: Image.Image = frame.to_image()
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.jpeg_quality)
                self.latest_frame = buf.getvalue()
                self.frame_event.set()
                self.frame_event.clear()
            except Exception as err:
                logger.warning("Fel vid mottagning/konvertering av videoframe: %s", err)
                break

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        """Serverar MJPEG-ström (compatible med Home Assistant mjpeg integration)."""
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Connection": "close",
            },
        )
        await response.prepare(request)

        logger.debug("Klient ansluten till MJPEG-ström från %s", request.remote)
        try:
            while True:
                if self.latest_frame is None:
                    await asyncio.sleep(0.1)
                    continue

                frame_bytes = self.latest_frame
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame_bytes)).encode("utf-8") + b"\r\n\r\n"
                )
                await response.write(header + frame_bytes + b"\r\n")
                await asyncio.sleep(self.fps_interval if self.fps_interval > 0 else 0.03)
        except (ConnectionResetError, asyncio.CancelledError):
            logger.debug("MJPEG-streamklient frånkopplad (%s)", request.remote)
        except Exception as err:
            logger.warning("Ett fel uppstod under MJPEG-strömning: %s", err)
        return response

    async def handle_snapshot(self, request: web.Request) -> web.Response:
        """Returnerar den senaste JPEG-bildrutan som en enskild bild."""
        if self.latest_frame is None:
            return web.Response(status=503, text="Kamera eller videoström ännu inte redo.")
        return web.Response(
            body=self.latest_frame,
            content_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    async def handle_status(self, request: web.Request) -> web.Response:
        """Returnerar statusinformation om kamerabryggan."""
        status = {
            "status": "online" if self.connected and self.latest_frame else "offline",
            "connected": self.connected,
            "has_frame": self.latest_frame is not None,
            "received_frames_count": self.received_frames_count,
            "printer_ip": self.printer_ip,
            "target_fps": self.target_fps,
            "jpeg_quality": self.jpeg_quality,
        }
        return web.json_response(status)

async def start_app():
    cfg = load_config()
    logger.info("Startar Creality K1 WebRTC Camera Bridge med konfiguration:")
    logger.info("  Skrivar-IP: %s", cfg["printer_ip"])
    logger.info("  Skrivarport: %s", cfg["printer_port"])
    logger.info("  Strömport (HTTP): %s", cfg["stream_port"])
    logger.info("  Mål FPS: %s", cfg["target_fps"])
    logger.info("  JPEG-kvalitet: %s", cfg["jpeg_quality"])

    bridge = CameraBridge(cfg)

    app = web.Application()
    app.router.add_get("/", bridge.handle_stream)
    app.router.add_get("/stream", bridge.handle_stream)
    app.router.add_get("/snapshot", bridge.handle_snapshot)
    app.router.add_get("/snapshot.jpg", bridge.handle_snapshot)
    app.router.add_get("/status", bridge.handle_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg["stream_port"])
    await site.start()
    logger.info("MJPEG HTTP-server igång på http://0.0.0.0:%s/", cfg["stream_port"])

    # Starta WebRTC-anslutningen som bakgrundstask och vänta på att servern körs
    webrtc_task = asyncio.create_task(bridge.connect_webrtc())
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(start_app())
    except KeyboardInterrupt:
        logger.info("Stänger av kamerabryggan...")
