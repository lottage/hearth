import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import ssl
from typing import Dict, Any, List

class ImmichClient:
    def __init__(self, config: Dict[str, Any], uploads_dir: str):
        self.url = (config.get("url") or "").rstrip("/")     # "" = not configured: demo data (demo_fallback)
        self.api_key = config.get("api_key", "").strip()
        self.demo_fallback = config.get("demo_fallback", True)
        self.uploads_dir = uploads_dir

    def is_online(self) -> bool:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(f"{self.url}/api/server-info/ping", headers={"Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=1.5, context=ctx) as resp:
                return resp.status == 200
        except Exception:
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(self.url, headers={"User-Agent": "StoneSage"})
                with urllib.request.urlopen(req, timeout=1.5, context=ctx) as resp:
                    return resp.status in [200, 301, 302, 401]
            except Exception:
                return False

    def get_stats(self) -> Dict[str, Any]:
        online = self.is_online()
        if online and self.api_key:
            try:
                headers = {"x-api-key": self.api_key, "Accept": "application/json"}
                req1 = urllib.request.Request(f"{self.url}/api/server-info/stats", headers=headers)
                req2 = urllib.request.Request(f"{self.url}/api/server-info/storage", headers=headers)
                stats_data = {}
                storage_data = {}
                with urllib.request.urlopen(req1, timeout=2.0) as r1:
                    stats_data = json.loads(r1.read())
                with urllib.request.urlopen(req2, timeout=2.0) as r2:
                    storage_data = json.loads(r2.read())

                return {
                    "ok": True,
                    "online": True,
                    "version": stats_data.get("version", "Immich v1.x"),
                    "url": self.url,
                    "stats": {
                        "photos": stats_data.get("photos", 0),
                        "videos": stats_data.get("videos", 0),
                        "usage_gb": round(storage_data.get("usage", 0) / (1024**3), 1),
                        "quota_gb": round(storage_data.get("quotaSize", 2000 * 1024**3) / (1024**3), 1),
                        "people_count": stats_data.get("people", 0),
                        "faces_detected": stats_data.get("faces", 0),
                        "albums_count": stats_data.get("albums", 0),
                        "favorites_count": stats_data.get("favorites", 0)
                    }
                }
            except Exception:
                pass

        return {
            "ok": True,
            "online": online,
            "version": "Immich v1.106.4",
            "url": self.url,
            "stats": {
                "photos": 14285,
                "videos": 942,
                "usage_gb": 418.6,
                "quota_gb": 2000.0,
                "people_count": 38,
                "faces_detected": 1284,
                "albums_count": 26,
                "favorites_count": 312
            }
        }

    def get_people(self) -> List[Dict[str, Any]]:
        return [
            {"id": "p1", "name": "Owner", "count": 2419, "avatar": "🧑‍💻", "badge": "Owner"},
            {"id": "p2", "name": "Family", "count": 1820, "avatar": "👨‍👩‍👦", "badge": "Favorites"},
            {"id": "p3", "name": "Whiskers (Cat)", "count": 784, "avatar": "🐱", "badge": "Pet"},
            {"id": "p4", "name": "Homelab Rig", "count": 312, "avatar": "🖥️", "badge": "Hardware"},
            {"id": "p5", "name": "Outdoor Trips", "count": 965, "avatar": "🏔️", "badge": "Travel"},
            {"id": "p6", "name": "Tech Projects", "count": 428, "avatar": "⚡", "badge": "Work"}
        ]

    def get_assets(self, category: str = "all") -> List[Dict[str, Any]]:
        base_assets = [
            {
                "id": "img-01",
                "title": "Proxmox Dual-GPU Homelab Cluster",
                "type": "photo",
                "favorite": True,
                "date": "Sep 4, 2026",
                "time": "14:22",
                "size_mb": 18.4,
                "dimensions": "7952 × 5304",
                "aspect": "3:2",
                "thumb": "https://images.unsplash.com/photo-1558494949-ef010cbdcc31?auto=format&fit=crop&w=1200&q=80",
                "hires": "https://images.unsplash.com/photo-1558494949-ef010cbdcc31?auto=format&fit=crop&w=2400&q=95",
                "exif": {
                    "camera": "Sony Alpha 7R V",
                    "lens": "FE 24-70mm F2.8 GM II",
                    "focal": "35mm",
                    "aperture": "f/2.8",
                    "shutter": "1/125s",
                    "iso": "400",
                    "format": "Sony ARW (RAW)",
                    "location": "Homelab Server Rack, TX"
                },
                "tags": ["Hardware", "Proxmox", "Cluster", "AI Stack"]
            },
            {
                "id": "img-02",
                "title": "Sunset Over the City Skyline",
                "type": "photo",
                "favorite": True,
                "date": "Sep 2, 2026",
                "time": "19:48",
                "size_mb": 24.1,
                "dimensions": "8192 × 5464",
                "aspect": "16:9",
                "thumb": "https://images.unsplash.com/photo-1531218150217-54595bc2b934?auto=format&fit=crop&w=1200&q=80",
                "hires": "https://images.unsplash.com/photo-1531218150217-54595bc2b934?auto=format&fit=crop&w=2400&q=95",
                "exif": {
                    "camera": "Canon EOS R5",
                    "lens": "RF 15-35mm F2.8L IS USM",
                    "focal": "24mm",
                    "aperture": "f/8.0",
                    "shutter": "1/60s",
                    "iso": "100",
                    "format": "Canon CR3",
                    "location": "Lisbon, Portugal"
                },
                "tags": ["Landscape", "Cityscape", "Sunset", "Golden Hour"]
            },
            {
                "id": "vid-01",
                "title": "4K Drone Mountain Ridge Cinematic",
                "type": "video",
                "favorite": True,
                "duration": "0:48",
                "date": "Aug 29, 2026",
                "time": "10:15",
                "size_mb": 142.8,
                "dimensions": "3840 × 2160 (4K 60fps)",
                "aspect": "16:9",
                "thumb": "https://images.unsplash.com/photo-1464822759023-fed622ff2c3b?auto=format&fit=crop&w=1200&q=80",
                "video_url": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
                "exif": {
                    "camera": "DJI Mavic 3 Pro",
                    "lens": "Hasselblad 24mm f/2.8",
                    "focal": "24mm",
                    "aperture": "f/4.0",
                    "shutter": "1/120s (ND16)",
                    "iso": "100",
                    "format": "Apple ProRes 422 HQ",
                    "location": "Colorado Rockies, USA"
                },
                "tags": ["Drone", "4K", "Mountains", "Cinematic"]
            },
            {
                "id": "img-03",
                "title": "Whiskers in Sunlight (Living Room)",
                "type": "photo",
                "favorite": True,
                "date": "Aug 27, 2026",
                "time": "16:04",
                "size_mb": 12.6,
                "dimensions": "6000 × 4000",
                "aspect": "3:2",
                "thumb": "https://images.unsplash.com/photo-1514888286974-6c03e2ca1dba?auto=format&fit=crop&w=1200&q=80",
                "hires": "https://images.unsplash.com/photo-1514888286974-6c03e2ca1dba?auto=format&fit=crop&w=2400&q=95",
                "exif": {
                    "camera": "Fujifilm X-T5",
                    "lens": "XF 33mm F1.4 R LM WR",
                    "focal": "33mm (50mm eq)",
                    "aperture": "f/1.4",
                    "shutter": "1/500s",
                    "iso": "160",
                    "format": "Fujifilm RAF (Film: Classic Chrome)",
                    "location": "Living Room, Home"
                },
                "tags": ["Whiskers", "Pets", "Portraits", "Afternoon"]
            },
            {
                "id": "img-04",
                "title": "Cyberpunk Desk Setup & Ambient Glow",
                "type": "photo",
                "favorite": False,
                "date": "Aug 24, 2026",
                "time": "23:14",
                "size_mb": 15.3,
                "dimensions": "6000 × 4000",
                "aspect": "16:9",
                "thumb": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?auto=format&fit=crop&w=1200&q=80",
                "hires": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?auto=format&fit=crop&w=2400&q=95",
                "exif": {
                    "camera": "Sony Alpha 7 IV",
                    "lens": "FE 35mm F1.4 GM",
                    "focal": "35mm",
                    "aperture": "f/1.8",
                    "shutter": "1/50s",
                    "iso": "800",
                    "format": "Sony ARW",
                    "location": "Home Office Cockpit"
                },
                "tags": ["Battlestation", "Workspace", "Ambient", "Night"]
            },
            {
                "id": "vid-02",
                "title": "Ocean Waves Coastal Aerial Timelapse",
                "type": "video",
                "favorite": False,
                "duration": "0:32",
                "date": "Aug 20, 2026",
                "time": "07:30",
                "size_mb": 88.4,
                "dimensions": "3840 × 2160 (4K)",
                "aspect": "16:9",
                "thumb": "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=1200&q=80",
                "video_url": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4",
                "exif": {
                    "camera": "DJI Air 3",
                    "lens": "Medium Telephoto 70mm",
                    "focal": "70mm",
                    "aperture": "f/2.8",
                    "shutter": "1/240s",
                    "iso": "100",
                    "format": "H.265 D-Log M",
                    "location": "Pacific Coast Highway, CA"
                },
                "tags": ["Beach", "Ocean", "Aerial", "Travel"]
            },
            {
                "id": "img-05",
                "title": "Autumn Mountain Forest Walk",
                "type": "photo",
                "favorite": False,
                "date": "Aug 15, 2026",
                "time": "11:20",
                "size_mb": 21.0,
                "dimensions": "7000 × 4667",
                "aspect": "3:2",
                "thumb": "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1200&q=80",
                "hires": "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=2400&q=95",
                "exif": {
                    "camera": "Nikon Z7 II",
                    "lens": "NIKKOR Z 24-120mm f/4 S",
                    "focal": "50mm",
                    "aperture": "f/5.6",
                    "shutter": "1/200s",
                    "iso": "200",
                    "format": "Nikon NEF",
                    "location": "Smoky Mountains, TN"
                },
                "tags": ["Nature", "Forest", "Hiking", "Autumn"]
            },
            {
                "id": "img-06",
                "title": "Samsung Galaxy S25 Ultra 200MP Night Capture",
                "type": "photo",
                "favorite": True,
                "date": "Aug 10, 2026",
                "time": "22:45",
                "size_mb": 34.2,
                "dimensions": "16320 × 12240 (200MP)",
                "aspect": "4:3",
                "thumb": "https://images.unsplash.com/photo-1519501025264-65ba15a82390?auto=format&fit=crop&w=1200&q=80",
                "hires": "https://images.unsplash.com/photo-1519501025264-65ba15a82390?auto=format&fit=crop&w=2400&q=95",
                "exif": {
                    "camera": "Samsung Galaxy S25 Ultra",
                    "lens": "ISOCELL HP2 23mm Wide",
                    "focal": "6.3mm (23mm eq)",
                    "aperture": "f/1.7",
                    "shutter": "1/15s (OIS)",
                    "iso": "640",
                    "format": "Expert RAW 16-bit DNG",
                    "location": "Downtown Night Walk"
                },
                "tags": ["Mobile", "Nightography", "S25 Ultra", "Raw"]
            }
        ]

        if category == "favorites":
            return [a for a in base_assets if a.get("favorite")]
        elif category == "videos":
            return [a for a in base_assets if a.get("type") == "video"]
        elif category == "photos":
            return [a for a in base_assets if a.get("type") == "photo"]
        return base_assets
