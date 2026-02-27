#!/usr/bin/env python3
import json
import math
import time
from datetime import datetime, timezone

from sense_hat import SenseHat
import paho.mqtt.client as mqtt

RED    = [255, 0, 0]     # THEFT
YELLOW = [255, 255, 0]   # TEMP
BLUE   = [0, 0, 255]     # HUM
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
            if x in (0,7) or y in (0,7):
                px.append(color)
            else:
                px.append(inner)
    return px

class MuseumGuard:
    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            self.cfg = json.load(f)

        self.t_max = float(self.cfg["t_max_c"])
        self.rh_max = float(self.cfg["rh_max_pct"])
        self.theft_thr = float(self.cfg["theft_accel_g_threshold"])

        base = f"museumguard/{self.cfg['museum']}/{self.cfg['room']}/{self.cfg['artwork']}"
        self.topic_telemetry = f"{base}/telemetry"
        self.topic_alarm = f"{base}/alarm"

        self.sense = SenseHat()
        self.sense.clear()

        self.client = mqtt.Client(client_id=self.cfg["mqtt"]["client_id"])
        self.client.connect(self.cfg["mqtt"]["host"], int(self.cfg["mqtt"]["port"]), 60)
        self.client.loop_start()

        self._last_alarm_ts = 0.0

    def accel_magnitude_g(self) -> float:
        a = self.sense.get_accelerometer_raw()
        return math.sqrt(a["x"]**2 + a["y"]**2 + a["z"]**2)

    def publish_alarm(self, alarm_type: str, value: float, threshold: float, severity: str):
        payload = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": alarm_type,
            "severity": severity,
            "value": round(value, 3),
            "threshold": threshold
        })
        # qos=1 como en tu borrador
        self.client.publish(self.topic_alarm, payload, qos=1)

    def show_alarm(self, alarm_type: str):
        # patrones distintos para que se distingan a simple vista
        if alarm_type == "THEFT":
            # parpadeo rojo rápido
            self.sense.set_pixels(checkerboard(RED, OFF))
            time.sleep(0.15)
            self.sense.clear(RED)
            time.sleep(0.15)
        elif alarm_type == "TEMP":
            # borde amarillo
            self.sense.set_pixels(border(YELLOW))
        elif alarm_type == "HUM":
            # tablero azul
            self.sense.set_pixels(checkerboard(BLUE, OFF))
        else:
            self.sense.clear()

    def run(self):
        print("MuseumGuard iniciado.")
        print("Telemetry:", self.topic_telemetry)
        print("Alarm:", self.topic_alarm)

        last_telemetry = 0.0
        period = float(self.cfg["telemetry_period_s"])

        try:
            while True:
                now = time.time()
                t_c = self.sense.get_temperature()
                rh = self.sense.get_humidity()
                ag = self.accel_magnitude_g()

                alarm_type = None

                # Prioridad: THEFT > TEMP > HUM
                if ag > self.theft_thr:
                    alarm_type = "THEFT"
                    self.show_alarm(alarm_type)
                    # evita spamear alarmas a cada iteración
                    if now - self._last_alarm_ts > 1.0:
                        self.publish_alarm("THEFT", ag, self.theft_thr, "CRITICAL")
                        self._last_alarm_ts = now
                elif t_c > self.t_max:
                    alarm_type = "TEMP"
                    self.show_alarm(alarm_type)
                    if now - self._last_alarm_ts > 3.0:
                        self.publish_alarm("TEMP", t_c, self.t_max, "WARNING")
                        self._last_alarm_ts = now
                elif rh > self.rh_max:
                    alarm_type = "HUM"
                    self.show_alarm(alarm_type)
                    if now - self._last_alarm_ts > 3.0:
                        self.publish_alarm("HUM", rh, self.rh_max, "WARNING")
                        self._last_alarm_ts = now
                else:
                    self.sense.clear()

                # telemetría periódica
                if now - last_telemetry >= period:
                    payload = json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "t_c": round(t_c, 2),
                        "rh_pct": round(rh, 2),
                        "accel_g": round(ag, 3)
                    })
                    self.client.publish(self.topic_telemetry, payload, qos=0)
                    last_telemetry = now

                time.sleep(0.2)

        except KeyboardInterrupt:
            self.sense.clear()
            self.client.loop_stop()
            self.client.disconnect()
            print("\nApagando MuseumGuard...")

if __name__ == "__main__":
    MuseumGuard("config.json").run()