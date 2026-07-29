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
from aiortc.rtcdtlstransport import RTCDtlsTransport

try:
    from aiortc.rtp import RtcpPsfbPacket
except ImportError:
    RtcpPsfbPacket = None

from PIL import Image, ImageDraw

# Force unbuffered stdout logging
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)



def create_placeholder_frame(status_text: str = "Creality K1 Max WebRTC Bridge - Ansluten, väntar på ström...") -> bytes:
    """Skapar en mörk JPEG-bild med statustext så Home Assistant sätter kameran som Aktiv."""
    img = Image.new("RGB", (640, 360), color=(20, 24, 33))
    draw = ImageDraw.Draw(img)
    draw.ellipse((40, 165, 70, 195), fill=(0, 200, 100))
    draw.text((85, 172), status_text, fill=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()

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

    if len(sys.argv) > 1:
        config["stream_port"] = int(sys.argv[1])

    return config

log_level_map = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

logger = logging.getLogger("creality_webrtc")

def prepare_offer_sdp(sdp: str) -> str:
    """Rensar offer SDP och tvingar fram giltig RFC4566-struktur med m=video 9, Payload Type 106 & 96 samt a=setup:active."""
    lines = sdp.splitlines()
    new_lines = []
    cand_idx = 0
    
    for line in lines:
        if line.startswith("a=candidate:"):
            if " 172." in line or " fd" in line or " srflx" in line:
                continue
            parts = line.split(" ")
            if len(parts) >= 6:
                # Formatera som Creality Print (a=candidate:0 1 udp 2130706431 <IP> <PORT> typ host generation 0)
                new_lines.append(f"a=candidate:{cand_idx} 1 udp {parts[3]} {parts[4]} {parts[5]} typ host generation 0")
                cand_idx += 1
            else:
                new_lines.append(line)
        elif line.startswith("m=video"):
            parts = line.split(" ")
            new_lines.append(f"{parts[0]} 9 {parts[2]} 106 96 " + " ".join(parts[3:]))
        elif line.startswith("a=setup:"):
            new_lines.append("a=setup:active")
        elif line.startswith("a=msid-semantic:"):
            new_lines.append("a=msid-semantic: WMS metaRTCLiveStream")
        elif line.startswith("t="):
            new_lines.append(line)
            new_lines.append("a=ice-lite")
        else:
            new_lines.append(line)

    sdp_out = "\r\n".join(new_lines) + "\r\n"
    if "a=rtpmap:106" not in sdp_out:
        sdp_out += "a=rtpmap:106 H264/90000\r\na=fmtp:106 profile-level-id=42e01f;packetization-mode=1\r\n"
    if "a=rtpmap:96" not in sdp_out:
        sdp_out += "a=rtpmap:96 H264/90000\r\na=fmtp:96 profile-level-id=42e01f;packetization-mode=1\r\n"
    if "a=rtcp-rsize" not in sdp_out:
        sdp_out += "a=rtcp-rsize\r\n"
    if "a=rtcp-fb:106 transport-cc" not in sdp_out:
        sdp_out += "a=rtcp-fb:106 transport-cc\r\na=rtcp-fb:106 nack\r\na=rtcp-fb:106 nack pli\r\n"
    return sdp_out

def sanitize_sdp(sdp: str) -> str:
    """Sanerar SDP-svaret från skrivaren: slår ihop dubblerade a=fmtp och a=rtpmap rader samt tar bort dubbla PT i m=video."""
    lines = sdp.splitlines()
    new_lines = []
    seen_rtpmap = set()
    seen_fmtp = set()

    for line in lines:
        if line.startswith("m=video"):
            parts = line.split(" ")
            pts = list(dict.fromkeys(parts[3:]))
            new_lines.append(f"{parts[0]} {parts[1]} {parts[2]} " + " ".join(pts))
        elif line.startswith("a=rtpmap:"):
            m = re.match(r'^a=rtpmap:(\d+)', line)
            if m:
                pt = m.group(1)
                if pt in seen_rtpmap:
                    continue
                seen_rtpmap.add(pt)
            new_lines.append(line)
        elif line.startswith("a=fmtp:"):
            m = re.match(r'^a=fmtp:(\d+)', line)
            if m:
                pt = m.group(1)
                if pt in seen_fmtp:
                    continue
                seen_fmtp.add(pt)
            new_lines.append(line)
        else:
            new_lines.append(line)

    sanitized = "\r\n".join(new_lines) + "\r\n"
    logger.info("--- SANERAD SDP ANSWER FÖR AIORTC ---\n%s", sanitized)
    return sanitized

async def trigger_camera_via_ws(printer_ip: str):
    """Skickar initieringskommandon till skrivaren (GET /info på port 80 samt WebSocket getToken på port 9999)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://{printer_ip}/info", timeout=2.0) as resp:
                logger.info("HTTP /info anrop mot port 80 svarade HTTP %s", resp.status)
    except Exception as err:
        logger.debug("HTTP /info anrop fel: %s", err)

    ws_url = f"ws://{printer_ip}:9999/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, timeout=3.0) as ws:
                logger.info("Skickar getToken & camera init till skrivarens kontroll-WebSocket på port 9999...")
                cmd1 = {"method": "get", "params": {"reqGcodeFile": 1, "reqGcodeList": 1, "reqMaterials": 1, "boxsInfo": 1, "boxConfig": 1, "getToken": 1}}
                await ws.send_str(json.dumps(cmd1))
                await ws.send_str(json.dumps({"cmd": "video_start"}))
                await asyncio.sleep(0.5)
    except Exception as err:
        logger.debug("WebSocket-anrop på port 9999: %s", err)

class CameraBridge:
    def __init__(self, cfg: dict):
        self.printer_ip = cfg["printer_ip"]
        self.printer_port = cfg["printer_port"]
        self.stream_port = cfg["stream_port"]
        self.target_fps = cfg["target_fps"]
        self.jpeg_quality = cfg["jpeg_quality"]

        self.webrtc_url = f"http://{self.printer_ip}:{self.printer_port}/call/webrtc_local"
        self.placeholder_frame = create_placeholder_frame()
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
                self.received_frames_count = 0
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

                @self.pc.on("iceconnectionstatechange")
                def on_ice_change():
                    logger.info("WebRTC iceConnectionState: %s", self.pc.iceConnectionState)

                @self.pc.on("connectionstatechange")
                def on_conn_change():
                    logger.info("WebRTC connectionState: %s", self.pc.connectionState)

                offer = await self.pc.createOffer()
                await self.pc.setLocalDescription(offer)

                # Vänta på att ICE-gathering slutförs så alla lokala kandidater (IPs) inkluderas i offer
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

                # Filtrera bort Docker-interna subnät från offer SDP och inkludera Payload Type 96
                offer_sdp = prepare_offer_sdp(self.pc.localDescription.sdp)
                logger.info("Lokal SDP Offer som skickas till skrivaren:\n%s", offer_sdp)

                # 2. Paketera i JSON & Base64-koda
                payload_json = {
                    "type": "offer",
                    "sdp": offer_sdp
                }
                b64_payload = base64.b64encode(json.dumps(payload_json).encode("utf-8")).decode("utf-8")

                # 3. Skicka till skrivaren via HTTP POST med Origin & User-Agent-headers för WebRTC-auktorisering
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    headers = {
                        "Content-Type": "text/plain",
                        "Origin": f"http://{self.printer_ip}",
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": f"http://{self.printer_ip}/"
                    }
                    async with session.post(self.webrtc_url, data=b64_payload, headers=headers) as resp:
                        if resp.status == 200:
                            raw_answer = await resp.text()
                            decoded_json = json.loads(base64.b64decode(raw_answer).decode("utf-8"))
                            
                            original_sdp = decoded_json["sdp"]

                            # Sanera SDP för kompabilitet med aiortc
                            sanitized_sdp = sanitize_sdp(original_sdp)

                            answer = RTCSessionDescription(sdp=sanitized_sdp, type=decoded_json["type"])
                            await self.pc.setRemoteDescription(answer)

                            # Register SSRC 1 and Payload Type 106/96 in RtpRouter & _codecs
                            for transceiver in self.pc.getTransceivers():
                                if transceiver.receiver:
                                    if transceiver.receiver.transport:
                                        router = transceiver.receiver.transport._rtp_router
                                        router.register_receiver(transceiver.receiver, ssrcs=[1], payload_types=[96, 106])

                                    codecs_dict = getattr(transceiver.receiver, "_RTCRtpReceiver__codecs", {})
                                    if 96 in codecs_dict and 106 not in codecs_dict:
                                        codecs_dict[106] = codecs_dict[96]
                                    elif 106 in codecs_dict and 96 not in codecs_dict:
                                        codecs_dict[96] = codecs_dict[106]

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
                try:
                    await self.pc.close()
                except Exception:
                    pass
                self.pc = None

            logger.info("Väntar 10 sekunder innan återanslutning för att undvika överbelastning av skrivaren...")
            await asyncio.sleep(10)

    async def _process_video_track(self, track):
        """Avkodar videoframes från WebRTC-spåret och konverterar till JPEG."""
        logger.info("Väntar på första bildrutan (Keyframe/IDR) från skrivarkameran...")
        
        # Skicka RTCP PLI periodiskt med ssrc=1 & ssrc=0 för skrivarens videospår
        async def request_keyframe_loop():
            while self.connected and self.received_frames_count == 0:
                try:
                    if self.pc and RtcpPsfbPacket is not None:
                        for transceiver in self.pc.getTransceivers():
                            if transceiver.receiver and hasattr(transceiver.receiver, "_send_rtcp"):
                                pli1 = RtcpPsfbPacket(fmt=1, ssrc=1, media_ssrc=1)
                                pli0 = RtcpPsfbPacket(fmt=1, ssrc=0, media_ssrc=1)
                                logger.debug("Skickar RTCP PLI (ssrc=1 & ssrc=0, media_ssrc=1)...")
                                await transceiver.receiver._send_rtcp(pli1)
                                await transceiver.receiver._send_rtcp(pli0)
                except Exception as err:
                    logger.warning("Kunde inte skicka RTCP PLI: %s", err)
                await asyncio.sleep(0.5)

        keyframe_task = asyncio.create_task(request_keyframe_loop())

        while self.connected:
            try:
                frame = await track.recv()
                self.received_frames_count += 1
                if self.received_frames_count == 1:
                    logger.info("🎉 Mottog FÖRSTA bildrutan från skrivarkameran! (Upplösning: %sx%s)", frame.width, frame.height)
                    keyframe_task.cancel()
                elif self.received_frames_count % 100 == 0:
                    logger.info("Tagit emot %s bildrutor hittills.", self.received_frames_count)

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
                frame_bytes = self.latest_frame if self.latest_frame is not None else self.placeholder_frame
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame_bytes)).encode("utf-8") + b"\r\n\r\n"
                )
                await response.write(header + frame_bytes + b"\r\n")
                await asyncio.sleep(self.fps_interval if self.fps_interval > 0 else 0.07)
        except (ConnectionResetError, asyncio.CancelledError):
            logger.debug("MJPEG-streamklient frånkopplad (%s)", request.remote)
        except Exception as err:
            logger.warning("Ett fel uppstod under MJPEG-strömning: %s", err)
        return response

    async def handle_snapshot(self, request: web.Request) -> web.Response:
        """Returnerar den senaste JPEG-bildrutan eller statusbild så Home Assistant visar Aktiv."""
        frame_bytes = self.latest_frame if self.latest_frame is not None else self.placeholder_frame
        return web.Response(
            body=frame_bytes,
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
            "status": "online" if self.connected else "standby",
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
    log_level = cfg.get("log_level", "info").lower()
    main_level = log_level_map.get(log_level, logging.INFO)

    logging.basicConfig(
        level=main_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )

    # Dämpa aiortc & aioice från att fylla loggen i onödan om inte log_level är 'trace'
    if log_level != "trace":
        logging.getLogger("aiortc").setLevel(logging.WARNING)
        logging.getLogger("aioice").setLevel(logging.WARNING)

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
