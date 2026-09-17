import os
import json, sys, time
import paho.mqtt.client as mqtt
HOST, PORTAL = os.environ["CERBO_HOST"], os.environ["CERBO_PORTAL"]
got = {}
def on_msg(c, u, m):
    try: v = json.loads(m.payload.decode()).get("value")
    except Exception: v = None
    got[m.topic.split("/", 2)[2]] = v
try:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except AttributeError:
    c = mqtt.Client()
c.on_message = on_msg
c.connect(HOST, 1883, 60)
for t in ("solarcharger/+/History/#", "solarcharger/+/ProductName", "solarcharger/+/CustomName", "solarcharger/+/Yield/#",
          "vebus/+/Energy/#", "system/0/Timers/#", "battery/+/History/#", "recbms/#", "battery/+/RecBms/Energy/#"):
    c.subscribe(f"N/{PORTAL}/{t}")
c.loop_start()
end = time.time() + 25
while time.time() < end:
    c.publish(f"R/{PORTAL}/keepalive", "")
    time.sleep(5)
c.loop_stop()
json.dump(got, open("mqtt_hist.json", "w"), indent=0)
print(len(got), "topics")
