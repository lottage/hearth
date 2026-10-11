"""
Presence corrections and recognition profiles (Residents & Pets cards: "✗ Wrong" and "Correct as ▾").

A sighting comes from one of two places, and a correction goes back to where it came from:
  frigate  ref = Frigate event id. Reject -> PUT /api/events/<id>/false_positive. Relabel -> sub_label, and for a
           person also moves that event's saved face attempts (faces/train/<id>-*.webp) into the person's face
           library, which is how Frigate's face recognition learns.
  sentry   ref = snapshot filename on the engine host (the name is its prefix). Reject/relabel run wildlife_admin.py there
           over SSH (args on stdin). Relabelling to a person also uploads the snapshot to Frigate's face library.
Profiles live in known_entities.json on the engine host (read by the presence hub and the wildlife sentry); a new person
also gets an (empty) Frigate face profile.
"""

import base64
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

from lodestone import endpoints
from typing import Any, Callable, Dict, List, Optional

from hearth.frigate_presence import EVENT_ID, norm_name


class PresenceCorrections:
    def __init__(self, ssh_host: str, admin_path: str, frigate_url: str, run: Callable[..., Any] = subprocess.run,
                 http: Optional[Callable[..., Any]] = None,
                 prepare_face: Optional[Callable[[bytes, str], bytes]] = None):
        self.ssh_host = ssh_host
        self.admin_path = admin_path
        self.frigate = frigate_url.rstrip("/")
        self.run = run
        self.http = http or self._http
        # (frame, person) -> the part of the frame to learn from: the server crops to that person, so the right face is
        # learned when several people are in view. Identity when not given.
        self.prepare_face = prepare_face or (lambda jpeg, name: jpeg)

    # ------------------------------------------------------------ transport ----
    def _admin(self, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """wildlife_admin.py on the engine host; the command is a fixed word, everything user-typed goes on stdin as JSON."""
        res = self.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", endpoints.check_ssh_target(self.ssh_host),
                        f"python3 {self.admin_path} {command}"],
                       input=json.dumps(args), capture_output=True, text=True, timeout=20)
        try:
            return json.loads(res.stdout.strip().splitlines()[-1])
        except Exception:
            return {"ok": False, "error": (res.stderr or res.stdout or "no output").strip()[:300]}

    def _http(self, method: str, path: str, body: Optional[bytes] = None, ctype: str = "application/json") -> Any:
        req = urllib.request.Request(self.frigate + path, data=body, method=method,
                                     headers={"Content-Type": ctype} if body is not None else {})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        return json.loads(data) if data else {}

    # -------------------------------------------------------------- profiles ----
    def profiles(self) -> Dict[str, Any]:
        return self._admin("profiles", {})

    def add_profile(self, args: Dict[str, Any]) -> Dict[str, Any]:
        res = self._admin("profile-add", {k: args.get(k) for k in ("name", "kind", "species", "role", "traits")})
        if res.get("ok") and args.get("kind") == "person":
            try:
                self.http("POST", f"/api/faces/{urllib.parse.quote(res['profile']['name'])}/create", b"")
                res["frigate_face_profile"] = True
            except Exception as e:
                res["frigate_face_profile"] = f"failed: {e}"
        return res

    def _is_person(self, name: str) -> bool:
        ents = (self.profiles().get("entities") or {})
        return any(norm_name(p.get("name", "")) == norm_name(name) for p in ents.get("people", []))

    def _canonical(self, name: str) -> Optional[str]:
        """The profile's own spelling ('alex' -> 'Alex'), or None if there is no such profile."""
        ents = (self.profiles().get("entities") or {})
        return next((e["name"] for g in ("people", "pets") for e in ents.get(g, []) if norm_name(e["name"]) == norm_name(name)), None)

    # ------------------------------------------------------------ corrections ----
    def correct(self, source: str, ref: str, action: str, name: str = "") -> Dict[str, Any]:
        if action not in ("reject", "relabel"):
            return {"ok": False, "error": "action must be reject or relabel"}
        if action == "relabel":
            canon = self._canonical(name)
            if not canon:
                return {"ok": False, "error": f"unknown profile '{name}' (add it first)"}
            name = canon
        if source == "frigate":
            return self._correct_frigate(ref, action, name)
        if source == "sentry":
            return self._correct_sentry(ref, action, name)
        return {"ok": False, "error": "source must be frigate or sentry"}

    def _correct_frigate(self, event_id: str, action: str, name: str) -> Dict[str, Any]:
        if not EVENT_ID.match(event_id or ""):
            return {"ok": False, "error": "bad event id"}
        if action == "reject":
            self.http("PUT", f"/api/events/{event_id}/false_positive", b"")
            return {"ok": True, "rejected": event_id}
        self.http("POST", f"/api/events/{event_id}/sub_label", json.dumps({"subLabel": name, "subLabelScore": 1.0}).encode())
        out: Dict[str, Any] = {"ok": True, "relabelled": event_id, "name": name}
        if self._is_person(name):
            out["faces_trained"] = self._train_from_attempts(event_id, name)
        return out

    def _train_from_attempts(self, event_id: str, name: str) -> int:
        """Move the face crops Frigate saved while tracking this event into the person's library."""
        attempts: List[str] = [f for f in (self.http("GET", "/api/faces") or {}).get("train", []) if f.startswith(f"{event_id}-")]
        for f in attempts:
            self.http("POST", f"/api/faces/train/{urllib.parse.quote(name)}/classify", json.dumps({"training_file": f}).encode())
        return len(attempts)

    def _correct_sentry(self, file: str, action: str, name: str) -> Dict[str, Any]:
        if action == "reject":
            return self._admin("reject", {"file": file})
        res = self._admin("relabel", {"file": file, "name": name})
        if res.get("ok") and self._is_person(name):
            res["face_registered"] = self._register_face(res["file"], name)
        return res

    def _register_face(self, file: str, name: str) -> Any:
        """Upload a corrected sentry snapshot to Frigate's face library (Frigate finds the face in it)."""
        snap = self._admin("snapshot", {"file": file})
        if not snap.get("ok"):
            return f"failed: {snap.get('error')}"
        return self.register_face_image(base64.b64decode(snap["jpeg_b64"]), name, file)

    def register_face_image(self, jpeg: bytes, name: str, filename: str = "correction.jpg") -> Any:
        """Any corrected frame (sentry snapshot, patrol frame) into the person's Frigate face library."""
        try:
            jpeg = self.prepare_face(jpeg, name)
        except Exception:
            pass    # no crop: Frigate still looks for a face in the whole frame
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n").encode() + jpeg + f"\r\n--{boundary}--\r\n".encode()
        try:
            res = self.http("POST", f"/api/faces/{urllib.parse.quote(name)}/register", body, f"multipart/form-data; boundary={boundary}")
            return bool(res.get("success", True))
        except urllib.error.HTTPError as e:  # e.g. no face found in the frame
            return f"no face used ({e.code})"
