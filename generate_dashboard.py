#!/usr/bin/env python3
"""
Generate a Dreame vacuum dashboard for Home Assistant from your own
`dreame_vacuum` entities — no hand-editing of entity IDs.

It reads your entities from the HA REST API and writes:
  - vacuum.yaml   (paste into a dashboard's Raw configuration editor)

Usage:
  HA_URL=http://homeassistant.local:8123 HA_TOKEN=xxxx python3 generate_dashboard.py
or:
  python3 generate_dashboard.py --url http://homeassistant.local:8123 --token xxxx

The vacuum, its map camera, battery, cleaning history, and rooms are all
discovered automatically. The dashboard needs the HACS cards "Mushroom" and
"card-mod". PyYAML is used for prettier output if installed; otherwise valid
JSON (which Home Assistant also accepts) is written.
"""
import argparse, json, os, sys, urllib.request, urllib.error
try:
    import yaml  # optional, prettier output
    HAVE_YAML = True
except Exception:
    HAVE_YAML = False

# ---------------------------------------------------------------- HA API
def get_states(url, token):
    req = urllib.request.Request(url.rstrip("/") + "/api/states",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HA API error {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        sys.exit(f"Could not reach HA at {url}: {e}")

# ---------------------------------------------------------------- helpers
def room_icon(name):
    n = name.lower()
    if any(w in n for w in ("bath", "shower", "toilet", "powder")): return "mdi:shower"
    if "kitchen" in n: return "mdi:countertop"
    if any(w in n for w in ("dining", "dinning")): return "mdi:silverware-fork-knife"
    if any(w in n for w in ("living", "lounge", "family", "great")): return "mdi:sofa"
    if any(w in n for w in ("office", "study", "desk", "work")): return "mdi:desk"
    if any(w in n for w in ("bed", "master", "primary", "guest", "nursery")): return "mdi:bed"
    if any(w in n for w in ("laundry", "utility", "mud")): return "mdi:washing-machine"
    if any(w in n for w in ("hall", "entry", "foyer", "corridor", "stair")): return "mdi:floor-plan"
    if "garage" in n: return "mdi:garage"
    if any(w in n for w in ("closet", "wardrobe")): return "mdi:hanger"
    if any(w in n for w in ("dining room", "dinette")): return "mdi:silverware-fork-knife"
    return "mdi:floor-plan"

NOBG = {"style": "ha-card { background: none !important; box-shadow: none !important; border: none !important; }\n"}
CONTAINER = {"style": ("#root {\n  background: var(--card-background-color);\n  border: 1px solid var(--divider-color);\n"
                       "  border-radius: 16px;\n  padding: 8px 4px;\n}\n")}

# Next scheduled run, computed from the vacuum's own `schedule` attribute.
NEXT = ("{% set sched=state_attr('__V__','schedule') %}{% set ns=namespace(best=none) %}"
        "{% if sched %}{% for s in sched %}{% if s.enabled and not s.invalid %}"
        "{% set h=s.time.split(':')[0]|int %}{% set m=s.time.split(':')[1]|int %}"
        "{% for d in range(0,8) %}{% set cand=(now()+timedelta(days=d)).replace(hour=h,minute=m,second=0,microsecond=0) %}"
        "{% set sidx=(cand.weekday()+1)%7 %}"
        "{% if s.repeats[sidx]=='1' and cand>now() and (ns.best is none or cand<ns.best) %}{% set ns.best=cand %}{% endif %}"
        "{% endfor %}{% endif %}{% endfor %}{% endif %}"
        "{% if ns.best %}{% set dd=(ns.best.date()-now().date()).days %}"
        "{% if dd==0 %}Today{% elif dd==1 %}Tomorrow{% else %}{{ ns.best.strftime('%A') }}{% endif %}"
        " at {{ ns.best.strftime('%-I:%M %p') }}{% else %}Not scheduled{% endif %}")
LAST = ("{% set t=states('__H__') %}"
        "{% if t not in ['unknown','unavailable','none',''] %}{{ relative_time(as_datetime(t)) }} ago{% else %}—{% endif %}")
STATUS_COLOR = ("{{ {'docked':'green','charging':'green','cleaning':'blue','returning':'blue',"
                "'paused':'orange','error':'red'}.get(states('__V__'),'grey') }}")
STATUS_TEXT = "{{ state_attr('__V__','status') or states('__V__')|title }}"
BATT_COLOR = "{% set b=states('__B__')|int(0) %}{{ 'green' if b>50 else 'orange' if b>20 else 'red' }}"
STOP_VIS = (":host { {% if states('__V__') not in ['cleaning','returning','paused'] %}display:none !important;{% endif %} }\n"
            "ha-card {\n  background:#e53935; border:none; border-radius:16px;\n"
            "  --primary-text-color:#fff; --card-primary-color:#fff; --icon-primary-color:#fff; font-weight:700;\n}\n")
ROOMNAME = ("{% set rs=state_attr('__V__','rooms') %}{% set ns=namespace(n='__FB__') %}"
            "{% if rs %}{% for m in rs.values() %}{% for r in m %}{% if r.id==__ID__ %}{% set ns.n=r.name %}{% endif %}"
            "{% endfor %}{% endfor %}{% endif %}{{ ns.n }}")

# ---------------------------------------------------------------- build
def build_config(states):
    by_id = {e["entity_id"]: e for e in states}
    vac = next((e for e in states if e["entity_id"].startswith("vacuum.")
                and e["attributes"].get("device_class") == "dreame_vacuum"), None)
    if vac is None:
        vac = next((e for e in states if e["entity_id"].startswith("vacuum.")
                    and isinstance(e["attributes"].get("rooms"), dict)), None)
    if vac is None:
        sys.exit("No dreame_vacuum entity found. Is the dreame_vacuum integration loaded?")
    V = vac["entity_id"]; prefix = V.split(".", 1)[1]
    def pick(*c): return next((x for x in c if x in by_id), None)
    BAT = pick(f"sensor.{prefix}_battery_level")
    HIST = pick(f"sensor.{prefix}_cleaning_history")
    CAM = pick(f"camera.{prefix}_map", f"camera.{prefix}")
    if CAM is None:
        CAM = next((e["entity_id"] for e in states if e["entity_id"].startswith("camera.")
                    and (isinstance(e["attributes"].get("rooms"), dict) or "calibration_points" in e["attributes"])), None)
    rooms, seen = [], set()
    for mp in (vac["attributes"].get("rooms") or {}).values():
        for r in mp:
            rid = r.get("id")
            if rid is None or rid in seen: continue
            seen.add(rid); rooms.append((rid, r.get("name") or f"Room {rid}"))
    rooms.sort(key=lambda t: t[1].lower())

    def rep(s, **kw):
        s = s.replace("__V__", V).replace("__H__", HIST or "").replace("__B__", BAT or "")
        for k, v in kw.items(): s = s.replace(k, str(v))
        return s

    def svc_chip(icon, content, service):
        return {"type": "template", "icon": icon, "content": content,
                "tap_action": {"action": "call-service", "service": service, "target": {"entity_id": V}}}

    # status indicators
    sci = [{"type": "template", "entity": V, "icon": "mdi:robot-vacuum",
            "icon_color": rep(STATUS_COLOR), "content": rep(STATUS_TEXT), "tap_action": {"action": "more-info"}}]
    if BAT:
        sci.append({"type": "template", "entity": BAT, "icon": "mdi:battery",
                    "icon_color": rep(BATT_COLOR), "content": "{{ states('%s') }}%%" % BAT,
                    "tap_action": {"action": "more-info"}})
    refresh_targets = [x for x in (V, CAM, HIST) if x]
    sci.append({"type": "template", "icon": "mdi:refresh", "content": "Refresh",
                "tap_action": {"action": "call-service", "service": "homeassistant.update_entity",
                               "target": {"entity_id": refresh_targets}}})
    status_chips = {"type": "custom:mushroom-chips-card", "alignment": "center", "chips": sci, "card_mod": NOBG}
    acts = [svc_chip("mdi:pause", "Pause", "vacuum.pause"),
            svc_chip("mdi:home-import-outline", "Send home", "vacuum.return_to_base"),
            svc_chip("mdi:map-marker", "Find it", "vacuum.locate")]
    # "Max suction" — only if the vacuum exposes fan/suction levels; uses the highest one.
    fans = vac["attributes"].get("fan_speed_list") or vac["attributes"].get("suction_level_list") or []
    if fans:
        acts.append({"type": "template", "icon": "mdi:fan-speed-3", "content": "Max suction",
                     "tap_action": {"action": "call-service", "service": "vacuum.set_fan_speed",
                                    "target": {"entity_id": V}, "data": {"fan_speed": fans[-1]}}})
    action_chips = {"type": "custom:mushroom-chips-card", "alignment": "center", "chips": acts, "card_mod": NOBG}
    status_container = {"type": "vertical-stack", "cards": [status_chips, action_chips], "card_mod": CONTAINER}

    # info tiles (Last cleaned only if a history sensor exists)
    tiles = []
    if HIST:
        tiles.append({"type": "custom:mushroom-template-card", "icon": "mdi:history", "icon_color": "blue",
                      "primary": "Last cleaned", "secondary": rep(LAST), "multiline_secondary": False,
                      "entity": HIST, "tap_action": {"action": "more-info"}})
    tiles.append({"type": "custom:mushroom-template-card", "icon": "mdi:calendar-clock", "icon_color": "indigo",
                  "primary": "Next cleaning", "secondary": rep(NEXT), "multiline_secondary": False,
                  "entity": V, "tap_action": {"action": "more-info"}})
    info = {"type": "grid", "columns": len(tiles), "square": False, "cards": tiles}

    start = {"type": "custom:mushroom-template-card", "primary": "Start cleaning",
             "secondary": "Clean the whole house", "icon": "mdi:play-circle", "icon_color": "white",
             "layout": "horizontal", "multiline_secondary": False,
             "tap_action": {"action": "call-service", "service": "vacuum.start", "target": {"entity_id": V}},
             "card_mod": {"style": ("ha-card {\n  background:#2e7d32; border:none; border-radius:16px;\n"
                                    "  --primary-text-color:#fff; --secondary-text-color:rgba(255,255,255,.9);"
                                    " --card-primary-color:#fff; font-weight:700;\n}\n")}}
    stop = {"type": "custom:mushroom-template-card", "primary": "Stop and return to dock",
            "icon": "mdi:stop-circle", "icon_color": "white", "layout": "horizontal", "multiline_secondary": False,
            "tap_action": {"action": "call-service", "service": "vacuum.return_to_base", "target": {"entity_id": V},
                           "confirmation": {"text": "Stop cleaning and send the vacuum back to its dock?"}},
            "card_mod": {"style": rep(STOP_VIS)}}

    top = {"type": "vertical-stack", "cards": [
        {"type": "custom:mushroom-title-card", "title": "Vacuum", "subtitle": "At a glance"},
        status_container, info, start, stop]}
    view_cards = [top]

    if CAM:
        view_cards.append({"type": "vertical-stack", "cards": [
            {"type": "custom:mushroom-title-card", "subtitle": "Map"},
            {"type": "picture-entity", "entity": CAM, "camera_view": "auto",
             "show_name": False, "show_state": False, "tap_action": {"action": "more-info"}}]})

    if rooms:
        rcards = []
        for rid, name in rooms:
            rcards.append({"type": "custom:mushroom-template-card",
                           "primary": rep(ROOMNAME, __ID__=rid, __FB__=name), "secondary": "Tap to clean",
                           "icon": room_icon(name), "icon_color": "blue",
                           "tap_action": {"action": "call-service", "service": "dreame_vacuum.vacuum_clean_segment",
                                          "target": {"entity_id": V}, "data": {"segments": [rid]},
                                          "confirmation": {"text": f"Start cleaning {name}?"}},
                           "card_mod": {"style": "ha-card { border:1px solid var(--divider-color); border-radius:16px; }\n"}})
        view_cards.append({"type": "vertical-stack", "cards": [
            {"type": "custom:mushroom-title-card", "subtitle": "Clean a room"},
            {"type": "grid", "columns": 2, "square": False, "cards": rcards}]})

    config = {"title": "Vacuum",
              "views": [{"title": "Vacuum", "path": "vacuum", "icon": "mdi:robot-vacuum", "cards": view_cards}]}
    return config, dict(vacuum=V, camera=CAM, battery=BAT, history=HIST, rooms=len(rooms))

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HA_URL"))
    ap.add_argument("--token", default=os.environ.get("HA_TOKEN"))
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    if not a.url or not a.token:
        sys.exit("Set HA_URL and HA_TOKEN (env or --url/--token).")
    config, found = build_config(get_states(a.url, a.token))
    header = ("# Dreame vacuum dashboard — generated for your entities.\n"
              "# Requires the HACS cards: Mushroom and card-mod.\n"
              "# Add a new dashboard -> Edit -> Raw configuration editor -> paste this file.\n")
    if HAVE_YAML:
        body = yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)
    else:
        body = json.dumps(config, indent=2)  # valid YAML too
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "vacuum.yaml")
    with open(path, "w") as f:
        f.write(header + body)
    print(f"Wrote {path}")
    print(f"  vacuum:  {found['vacuum']}")
    print(f"  camera:  {found['camera'] or '(none — map section skipped)'}")
    print(f"  battery: {found['battery'] or '(none — battery chip skipped)'}")
    print(f"  history: {found['history'] or '(none — last-cleaned tile skipped)'}")
    print(f"  rooms:   {found['rooms']}")

if __name__ == "__main__":
    main()
