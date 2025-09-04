# econ_watcher.py
import time, json, yaml, threading
from typing import List, Tuple
from datetime import datetime, timezone
from econ_providers.tradingeconomics_adapter import TEAdapter, EconEvent

# NLP (optionnel et tolérant aux erreurs)
try:
    from nlp_advanced import advanced_analyze  # ton module existant
    HAS_NLP = True
except Exception:
    advanced_analyze = None   # type: ignore
    HAS_NLP = False


class RateLimiter:
    """Token bucket simple: 10 req/s par défaut."""
    def __init__(self, rate_per_sec: int = 10):
        self.rate = max(1, rate_per_sec)
        self.tokens = float(self.rate)
        self.last = time.time()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last
            self.last = now
            # recharge des jetons
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            if self.tokens < 1.0:
                sleep_for = (1.0 - self.tokens) / self.rate
                time.sleep(max(sleep_for, 0.001))
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


ADAPTERS = {
    "TE": TEAdapter,
}


class EconWatcher:
    def __init__(self, cfg_path: str, heartbeat_sec: int = 300, rate_per_sec: int = 10):
        self.cfg_path = cfg_path
        self.adapters: List[Tuple[object, dict]] = []
        self.next_tick: dict[object, float] = {}
        self.limiter = RateLimiter(rate_per_sec)
        self.heartbeat_sec = heartbeat_sec
        self._last_hb = 0.0

    def load_sources(self):
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            rows = yaml.safe_load(f) or []
        self.adapters.clear()
        now = time.time()
        # On étale le départ pour éviter les rafales
        offset = 0.0
        for cfg in rows:
            prov = cfg.get("provider")
            cls = ADAPTERS.get(prov)
            if not cls:
                continue
            adp = cls(cfg)
            self.adapters.append((adp, cfg))
            poll = max(1, int(cfg.get("poll_every_sec", 60)))
            self.next_tick[adp] = now + offset
            offset += 0.05  # 50 ms d’écart entre chaque source

    def run_forever(self):
        self.load_sources()
        while True:
            now = time.time()

            # Heartbeat global
            if now - self._last_hb >= self.heartbeat_sec:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                print(f"[HB] macro alive status=ok ts={ts}", flush=True)
                self._last_hb = now

            for adp, cfg in self.adapters:
                if now >= self.next_tick.get(adp, 0):
                    poll = max(1, int(cfg.get("poll_every_sec", 60)))
                    self.next_tick[adp] = now + poll
                    try:
                        self.limiter.acquire()
                        events = adp.poll()
                        for ev in events:
                            self.emit_event(ev)
                    except Exception as e:
                        print(f"[HB] alive source={cfg.get('id','TECal')} status=err latency=— {e}", flush=True)

            time.sleep(0.05)  # loop légère (20 Hz)

    def _run_nlp(self, text: str, ev: EconEvent):
        """Appelle le NLP si dispo, sinon fallback."""
        if not HAS_NLP or advanced_analyze is None:
            # Fallback minimal si NLP indisponible
            return {
                "summary": text,
                "labels": [],
                "score": None,
                "notes": "nlp_disabled",
            }
        try:
            # N'UTILISE PAS context_days (non supporté). La signature commune
            # de ton projet accepte souvent bank= et context_items=.
            return advanced_analyze(
                text,
                bank=ev.country,
                context_items=[],
            )
        except TypeError as e:
            # Signature différente ? Repli sur l'appel minimal.
            try:
                return advanced_analyze(text)
            except Exception as e2:
                return {
                    "summary": text,
                    "labels": [],
                    "score": None,
                    "notes": f"nlp_error:{e2}",
                }
        except Exception as e:
            return {
                "summary": text,
                "labels": [],
                "score": None,
                "notes": f"nlp_error:{e}",
            }

    def emit_event(self, ev: EconEvent):
        # [DATA] (utilisé par l'UI)
        print(f"[DATA] {ev.title} source={ev.source_id} link={ev.link}", flush=True)

        # Prépare quelques faits utiles (affichables côté UI si besoin)
        te = ev.raw.get("te", {}) if isinstance(ev.raw, dict) else {}
        facts = {
            "indicator": ev.raw.get("indicator") if isinstance(ev.raw, dict) else None,
            "latest_value": te.get("LatestValue"),
            "previous_value": te.get("PreviousValue"),
            "unit": ev.raw.get("unit") if isinstance(ev.raw, dict) else None,
            "frequency": ev.raw.get("frequency") if isinstance(ev.raw, dict) else None,
            "country": ev.country,
            "group": ev.group,
        }
        text = (
            f"{ev.country} {facts.get('indicator')}: "
            f"latest={facts.get('latest_value')} prev={facts.get('previous_value')} "
            f"{facts.get('unit') or ''} ({facts.get('frequency') or ''})."
        ).strip()

        analysis = self._run_nlp(text, ev)

        payload = {
            "bank_id": ev.source_id,      # l’UI réutilise bank_id/bank
            "bank": ev.country,           # pays dans la colonne Banque
            "title": ev.title,
            "link": ev.link,
            "pubDate": ev.pubDate,
            "analysis": analysis,
            "facts": facts,               # on joint les faits côté payload
            "sources": [ev.link] if ev.link else [],
            "ts": ev.pubDate,
            "meta": {"country": ev.country, "group": ev.group, "provider": "TE"},
        }
        print("[ANALYSIS] " + json.dumps(payload, ensure_ascii=False), flush=True)
