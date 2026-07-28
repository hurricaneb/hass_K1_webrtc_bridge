# Home Assistant Add-on: Creality K1 WebRTC Camera Bridge

En Home Assistant OS Add-on (och fristående Python-bro) för att hämta videoströmen via WebRTC från **Creality K1** och **Creality K1 Max** med nyare firmware där mjpeg-streamer inte längre är tillgänglig.

Kameraströmen görs tillgänglig som en **MJPEG-videoström** samt enskilda **JPEG-snapshots** via HTTP, vilket enkelt kan läggas till i Home Assistant.

---

## 🚀 Funktioner

- **WebRTC Handskakning:** Ansluter direkt till skrivarens lokala WebRTC-slutpunkt (`/call/webrtc_local`).
- **Automatisk återanslutning:** Hanterar nätverksavbrott eller omstart av skrivaren automatiskt.
- **MJPEG & Snapshot HTTP-server:** Inbyggd webbserver som levererar `/stream` (MJPEG) och `/snapshot.jpg`.
- **Anpassningsbar:** Ändra mål-FPS och JPEG-bildkvalitet för att optimera nätverk och processoranvändning.
- **Home Assistant OS Add-on:** Färdigkonfigurerad för att köras direkt i Home Assistant OS.

---

## 🛠️ Installation i Home Assistant OS

### Alternativ 1: Som anpassat tilläggsbibliotek (Custom Repository)
1. Öppna Home Assistant.
2. Gå till **Inställningar** -> **Tillägg (Add-ons)** -> **Tilläggsbutik (Add-on Store)**.
3. Klicka på menyn längst upp till höger (tre punkter) och välj **Reposter (Repositories)**.
4. Lägg till webbadressen till detta GitHub-arkiv (`https://github.com/hurricaneb/hass_K1_webrtc_bridge`).
5. Sök efter **Creality K1 WebRTC Camera Bridge** i tilläggsbutiken, klicka på **Installera**.

### Alternativ 2: Lokal installation i `/addons/`
1. Kopiera projektets alla filer till mappen `/addons/creality_webrtc_bridge` på din Home Assistant-enhet (via t.ex. Samba share eller SSH).
2. Gå till **Tilläggsbutiken** och klicka på **Kontrollera om det finns nya tillägg**.
3. Installera **Creality K1 WebRTC Camera Bridge** under lokala tillägg.

---

## ⚙️ Konfiguration i Home Assistant Add-on

Under fliken **Konfiguration** i tillägget ställer du in:

```yaml
printer_ip: "192.168.10.161"  # IP-adressen till din Creality K1 / K1 Max
printer_port: 8000           # WebRTC-port på skrivaren (standard 8000)
stream_port: 8080            # Porten där MJPEG-strömmen exponeras
target_fps: 15               # Max antal bildrutor per sekund (fps)
jpeg_quality: 80             # JPEG-kvalitet (1-100)
log_level: "info"            # Loggnivå (debug, info, warning, error)
```

Spara konfigurationen och starta tillägget.

---

## 📷 Lägg till kameran i Home Assistant

Öppna din `configuration.yaml` i Home Assistant och lägg till följande integrering:

```yaml
camera:
  - platform: mjpeg
    name: Creality K1 Max Camera
    mjpeg_url: http://127.0.0.1:8080/stream
    still_image_url: http://127.0.0.1:8080/snapshot.jpg
```

*(Ersätt `127.0.0.1` med IP-adressen till din Home Assistant-server om tillägget inte körs med `host_network`)*.

---

## 💻 Kör fristående med Python

Om du vill köra skriptet fristående (utanför Home Assistant Add-on):

1. **Installera beroenden:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Starta skriptet:**
   ```bash
   PRINTER_IP="192.168.10.161" python3 creality_webrtc_bridge.py
   ```

---

## 📜 Licens

Projektet är licensierat under **GNU General Public License v3.0 (GPL-3.0)**. Se [LICENSE](LICENSE) för mer information.
