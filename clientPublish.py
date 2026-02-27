#!/usr/bin/env python3
import json
import math
import time
import socket
import uuid
from datetime import datetime, timezone
from typing import Optional

from sense_hat import SenseHat
import paho.mqtt.client as mqtt

# --- Colores ---
RED    = [255, 0, 0]       # THEFT
YELLOW = [255, 255, 0]     # TEMP
BLUE   = [0, 0, 255]       # HUM
ORANGE = [255, 128, 0]     # ACK REQUIRED
OFF    = [0, 0, 0]

def checkerboard(c1, c2):
    px = []
    for y in range(8):
        for x in range(8):
            px.append(c1 if (x + y) % 2 == 0 else c2)
    return px

def border(color, inner=OFF):
    px = []
    for y in range(8):
        for x in range(8):
            if x in (0, 7) or y in (0, 7):
                px.append(color)
            else:
                px.append(inner)
    return px

class MuseumGuard:
    STATE_NORMAL = "NORMAL"
    STATE_ALARM_HOLD = "ALARM_HOLD"
    STATE_ALARM_ACK = "ALARM_ACK"

    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            self.cfg = json.load(f)

        self.t_max = float(self.cfg["t_max_c"])
        self.rh_max = float(self.cfg["rh_max_pct"])
        self.theft_thr = float(self.cfg["theft_accel_g_threshold"])

        base = f"museumguard/{self.cfg['museum']}/{self.cfg['room']}/{self.cfg['artwork']}"
        self.topic_telemetry = f"{base}/telemetry"
        self.topic_alarm = f"{base}/alarm"
        self.topic_cmd = f"{base}/cmd"   # <-- aquí escuchamos comandos ACK/CLEAR

        self.sense = SenseHat()
        self.sense.clear()

        # --- Estado de alarma ---
        self.state = self.STATE_NORMAL
        self.current_alarm_type: Optional[str] = None  # "THEFT"|"TEMP"|"HUM"|None
        self.hold_until = 0.0
        self.hold_seconds = 10.0

        # Throttle publicación de alarmas
        self._last_alarm_pub_ts = 0.0

        # Flags de “ack” (por joystick o por MQTT)
        self._ack_requested = False

        # --- MQTT (client_id único + fuerza IPv4) ---
        host = self.cfg["mqtt"]["host"]
        port = int(self.cfg["mqtt"]["port"])

        host_ipv4 = socket.gethostbyname(host)
        base_id = self.cfg["mqtt"].get("client_id", "museumguard-node")
        unique_id = f"{base_id}-{uuid.uuid4().hex[:6]}"

        self._mqtt_connected = False
        self.client = mqtt.Client(client_id=unique_id, protocol=mqtt.MQTTv311)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        print(f"MQTT -> conectando a {host} ({host_ipv4}):{port} client_id={unique_id}")
        self.client.connect(host_ipv4, port, 60)
        self.client.loop_start()

        # Suscripción a comandos
        # (se suscribe aunque aún no esté conectado; se completa al conectar)
        # Para asegurar, lo hacemos también en on_connect.
        time.sleep(0.5)

    # ---------- MQTT callbacks ----------
    def _on_connect(self, client, userdata, flags, rc):
        print("MQTT CONNECT rc =", rc)
        self._mqtt_connected = (rc == 0)
        if rc == 0:
            client.subscribe(self.topic_cmd, qos=1)
            print("MQTT SUB cmd:", self.topic_cmd)

    def _on_disconnect(self, client, userdata, rc):
        print("MQTT DISCONNECT rc =", rc)
        self._mqtt_connected = False

    def _on_message(self, client, userdata, msg):
        # Aceptamos:
        # - "ACK" o "CLEAR"
        # - JSON {"action":"ack"} o {"action":"clear"}
        try:
            payload_raw = msg.payload.decode("utf-8", errors="ignore").strip()
        except Exception:
            payload_raw = ""

        action = payload_raw.upper()

        if payload_raw.startswith("{"):
            try:
                obj = json.loads(payload_raw)
                action = str(obj.get("action", "")).upper()
            except Exception:
                pass

        if action in ("ACK", "CLEAR", "RESET", "STOP"):
            print(f"CMD recibido en {msg.topic}: {payload_raw} -> ACK solicitado")
            self._ack_requested = True
        else:
            print(f"CMD ignorado en {msg.topic}: {payload_raw} (usa ACK/CLEAR o JSON {{\"action\":\"ack\"}})")

    # ---------- Sensores ----------
    def accel_magnitude_g(self) -> float:
        a = self.sense.get_accelerometer_raw()
        return math.sqrt(a["x"]**2 + a["y"]**2 + a["z"]**2)

    # ---------- Alarmas: publicación ----------
    def publish_alarm(self, alarm_type: str, value: float, threshold: float, severity: str, phase: str):
        payload = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": alarm_type,
            "severity": severity,
            "phase": phase,  # "TRIGGERED" | "ACK_REQUIRED" | "CLEARED"
            "value": round(value, 3),
            "threshold": threshold
        })
        self.client.publish(self.topic_alarm, payload, qos=1)

    # ---------- Display ----------
    def _display_alarm_hold(self, alarm_type: str, now: float):
        # SIN sleeps: parpadeo por tiempo
        if alarm_type == "THEFT":
            blink_on = (int(now * 5) % 2 == 0)  # ~5 Hz/2 -> visible
            self.sense.clear(RED if blink_on else OFF)
        elif alarm_type == "TEMP":
            self.sense.set_pixels(border(YELLOW))
        elif alarm_type == "HUM":
            self.sense.set_pixels(checkerboard(BLUE, OFF))
        else:
            self.sense.clear()

    def _display_ack_required(self, now: float):
        # Naranja fijo (o si quieres, parpadeo: alterna ORANGE/OFF)
        self.sense.clear(ORANGE)

    # ---------- Estado ----------
    def trigger_alarm(self, alarm_type: str, value: float, threshold: float, severity: str):
        now = time.time()

        # Si ya está en ACK y entra un THEFT nuevo, lo priorizamos y rearmamos hold
        # (también si cambia de tipo, rearmamos hold)
        is_escalation = (self.current_alarm_type != alarm_type)
        if self.state == self.STATE_NORMAL or is_escalation or alarm_type == "THEFT":
            self.current_alarm_type = alarm_type
            self.state = self.STATE_ALARM_HOLD
            self.hold_until = now + self.hold_seconds

        # Publica evento (con throttle para no spamear)
        min_gap = 1.0 if alarm_type == "THEFT" else 3.0
        if now - self._last_alarm_pub_ts > min_gap:
            self.publish_alarm(alarm_type, value, threshold, severity, phase="TRIGGERED")
            self._last_alarm_pub_ts = now

    def clear_alarm(self):
        if self.current_alarm_type is not None:
            # Evento de cierre
            try:
                self.publish_alarm(self.current_alarm_type, value=0.0, threshold=0.0, severity="INFO", phase="CLEARED")
            except Exception:
                pass

        self.state = self.STATE_NORMAL
        self.current_alarm_type = None
        self.hold_until = 0.0
        self._ack_requested = False
        self.sense.clear()

    def check_joystick_ack(self):
        # Reconocimiento por pulsación del joystick (middle)
        for ev in self.sense.stick.get_events():
            if ev.action == "pressed" and ev.direction == "middle":
                print("ACK por joystick (middle)")
                self._ack_requested = True

    def run(self):
        print("MuseumGuard iniciado.")
        print("Telemetry:", self.topic_telemetry)
        print("Alarm:", self.topic_alarm)
        print("Cmd:", self.topic_cmd)

        last_telemetry = 0.0
        period = float(self.cfg["telemetry_period_s"])

        try:
            while True:
                now = time.time()

                # 1) Leer joystick (ACK)
                self.check_joystick_ack()

                # 2) Si estamos en modo ACK y llega ACK (joystick o MQTT) => limpiar
                if self.state == self.STATE_ALARM_ACK and self._ack_requested:
                    self.clear_alarm()
                    # seguimos al siguiente loop
                    time.sleep(0.05)
                    continue

                # 3) Leer sensores (siempre, para telemetría; la lógica de alarma depende del estado)
                t_c = self.sense.get_temperature()
                rh = self.sense.get_humidity()
                ag = self.accel_magnitude_g()

                # 4) Si estamos en NORMAL o HOLD, evaluamos condiciones y disparamos si toca
                #    Si estamos en ACK, no quitamos alarma hasta ACK, pero sí permitimos ESCALACIÓN (THEFT)
                if self.state in (self.STATE_NORMAL, self.STATE_ALARM_HOLD):
                    if ag > self.theft_thr:
                        self.trigger_alarm("THEFT", ag, self.theft_thr, "CRITICAL")
                    elif t_c > self.t_max:
                        self.trigger_alarm("TEMP", t_c, self.t_max, "WARNING")
                    elif rh > self.rh_max:
                        self.trigger_alarm("HUM", rh, self.rh_max, "WARNING")
                elif self.state == self.STATE_ALARM_ACK:
                    # En ACK, si vuelve a ocurrir THEFT, rearmamos
                    if ag > self.theft_thr:
                        self.trigger_alarm("THEFT", ag, self.theft_thr, "CRITICAL")

                # 5) Actualizar display según estado
                if self.state == self.STATE_NORMAL:
                    self.sense.clear()
                elif self.state == self.STATE_ALARM_HOLD:
                    # Mostrar alarma y pasar a ACK cuando venza el hold
                    if self.current_alarm_type is not None:
                        self._display_alarm_hold(self.current_alarm_type, now)
                    if now >= self.hold_until:
                        # Pasamos a ACK REQUIRED
                        self.state = self.STATE_ALARM_ACK
                        self._ack_requested = False
                        if self.current_alarm_type is not None:
                            # Publica evento de que requiere ack
                            self.publish_alarm(self.current_alarm_type, value=0.0, threshold=0.0,
                                               severity="WARNING", phase="ACK_REQUIRED")
                elif self.state == self.STATE_ALARM_ACK:
                    self._display_ack_required(now)

                # 6) Telemetría periódica + print sin datetime
                if now - last_telemetry >= period:
                    print(f"T={t_c:.2f} ºC | RH={rh:.2f} % | accel={ag:.3f} g", flush=True)

                    payload = json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "t_c": round(t_c, 2),
                        "rh_pct": round(rh, 2),
                        "accel_g": round(ag, 3),
                        "state": self.state,
                        "alarm_type": self.current_alarm_type
                    })
                    self.client.publish(self.topic_telemetry, payload, qos=0)
                    last_telemetry = now

                time.sleep(0.1)

        except KeyboardInterrupt:
            self.sense.clear()
            self.client.loop_stop()
            self.client.disconnect()
            print("\nApagando MuseumGuard...")

if __name__ == "__main__":
    MuseumGuard("config.json").run()