"""Comandi Telegram interattivi (in italiano).

Il polling del bot appartiene a Home Assistant (telegram_bot): HA inoltra
i comandi al nostro endpoint REST `/telegram/cmd`, e TUTTA la logica vive
qui (il nostro stack). Le risposte partono direttamente dal nostro servizio
via Bot API. I file GCODE inviati in chat arrivano su `/telegram/file`
(file_id → download via getFile, che NON confligge col polling).

Comandi:
  /help /aiuto            elenco comandi
  /status /stato          stato + foto
  /foto                   ultimo frame webcam
  /ai                     metriche AI (rischio ML + CV + layer)
  /pause /pausa           metti in pausa
  /resume /riprendi       riprendi
  /stop                   ferma la stampa (richiede conferma: "/stop conferma")
  /file                   elenco file sulla stampante + locali
  /stampa <nome>          avvia la stampa (richiede conferma)
  /upload /carica         istruzioni per inviare un .gcode in chat

Sicurezza: accetta solo dalla chat configurata (TELEGRAM_CHAT_ID) — su HA
il telegram_bot processa comunque solo le allowed_chat_ids (doppio filtro).
I comandi distruttivi richiedono conferma entro 120 s.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("elegoo.tgcmd")

CONFIRM_TTL = 120.0
HELP_TEXT = (
    "🤖 <b>Comandi stampante 3D</b>\n"
    "/status o /stato — stato stampa con foto\n"
    "/foto — ultimo frame della webcam\n"
    "/ai — metriche del rilevatore (rischio ML, CV, layer)\n"
    "/pause o /pausa — metti in pausa\n"
    "/resume o /riprendi — riprendi la stampa\n"
    "/stop — ferma la stampa (chiede conferma)\n"
    "/velocita 80 — imposta la velocità di stampa (50-150%)\n"
    "/luce on|off — accendi/spegni la luce interna\n"
    "/link — link diretti a dashboard e webcam\n"
    "/file — elenco GCODE (stampante + locali)\n"
    "/stampa nome.gcode — avvia una stampa (chiede conferma)\n"
    "/upload — come caricare un file GCODE"
)


def _fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    m = int(seconds) // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


class TelegramCommandHandler:
    def __init__(self, cfg, state, printer_api, webcam, telegram, uploader, ai_monitor):
        self.cfg = cfg
        self.state = state
        self.printer_api = printer_api
        self.webcam = webcam
        self.telegram = telegram
        self.uploader = uploader
        self.ai = ai_monitor
        self.allowed_chat: Optional[str] = str(cfg.telegram.get("chat_id") or "") or None
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    def _authorized(self, chat_id) -> bool:
        return self.allowed_chat is not None and str(chat_id) == self.allowed_chat

    async def _reply(self, text: str, photo: Optional[bytes] = None,
                     kind: str = "tg_cmd") -> None:
        await self.telegram.notify(text, photo=photo, critical=True, kind=kind)

    async def _photo(self) -> Optional[bytes]:
        try:
            return await self.webcam.get_jpeg(max_age_s=6.0, wait_fresh=3.0)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    async def handle(self, text: str, chat_id) -> dict[str, Any]:
        """Punto d'ingresso: parsa il comando e risponde in chat."""
        if not self._authorized(chat_id):
            log.warning("Comando Telegram da chat non autorizzata: %s", chat_id)
            return {"ok": False, "reason": "chat non autorizzata"}
        text = (text or "").strip()
        low = text.lower()
        log.info("Comando Telegram: %s", text[:80])

        try:
            if low.startswith(("/help", "/aiuto", "/start")):
                await self._reply(HELP_TEXT, kind="tg_help")
            elif low.startswith(("/status", "/stato")):
                await self._cmd_status()
            elif low.startswith("/foto"):
                photo = await self._photo()
                await self._reply("📷 Frame webcam" if photo
                                  else "📷 Webcam non disponibile", photo=photo,
                                  kind="tg_foto")
            elif low.startswith("/ai"):
                await self._cmd_ai()
            elif low.startswith(("/pause", "/pausa")):
                await self._cmd_simple("pause", self.printer_api.pause_print,
                                       "⏸ Pausa", "⏸️ Pausa inviata correttamente.")
            elif low.startswith(("/resume", "/riprendi")):
                await self._cmd_simple("resume", self.printer_api.resume_print,
                                       "▶ Resume", "▶️ Ripresa inviata correttamente.")
            elif low.startswith(("/velocita", "/speed", "/vel")):
                await self._cmd_speed(text)
            elif low.startswith(("/luce", "/light")):
                await self._cmd_light(text)
            elif low.startswith(("/link", "/webcam", "/cam")):
                await self._cmd_link()
            elif low.startswith("/stop"):
                await self._cmd_stop(text)
            elif low.startswith(("/file", "/filelist", "/files")):
                await self._cmd_files()
            elif low.startswith(("/stampa", "/print")):
                await self._cmd_print(text)
            elif low.startswith(("/upload", "/carica")):
                await self._reply(
                    "📤 <b>Caricare un GCODE</b>\n"
                    "Invia il file .gcode direttamente in questa chat: verrà "
                    "salvato e trasferito alla stampante. Poi avvialo con "
                    "/stampa nomefile.gcode")
            else:
                await self._reply("Comando non riconosciuto. " + HELP_TEXT, kind="tg_help")
        except Exception as e:  # noqa: BLE001
            log.exception("Errore nel comando Telegram: %s", text)
            await self._reply(f"⚠️ Errore nell'eseguire il comando: {e}")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    async def _cmd_status(self) -> None:
        s = self.state.snapshot()
        t = s.get("temps") or {}
        lines = [
            f"🖨 <b>Stampante 3D</b> — {s.get('status', 'sconosciuta')}",
            f"File: {s.get('filename') or '—'}",
        ]
        if s.get("percent") is not None:
            lines.append(f"Avanzamento: {s['percent']:.1f}% — restano "
                         f"{_fmt_eta(s.get('time_remaining_s'))}")
        if s.get("elapsed_s"):
            lines.append(f"Trascorsi: {_fmt_eta(s['elapsed_s'])}")
        if s.get("total_layers"):
            lines.append(f"Layer: {s.get('current_layer')}/{s.get('total_layers')}")
        if t.get("nozzle") is not None:
            lines.append(f"Ugello: {t['nozzle']:.0f}°C (target {t.get('nozzle_target') or 0:.0f}) · "
                         f"Piatto: {t.get('hotbed', 0):.0f}°C")
        if s.get("last_error"):
            lines.append(f"⚠️ Ultimo errore: {s['last_error']}")
        if self.ai is not None and self.ai.ml is not None:
            risk = float(self.ai.ml.last_result.get("score", 0.0)) * 100
            lines.append(f"🤖 Rischio AI: {risk:.1f}%")
        await self._reply("\n".join(lines), photo=await self._photo(), kind="tg_status")

    async def _cmd_ai(self) -> None:
        if self.ai is None:
            await self._reply("AI non inizializzata")
            return
        st = self.ai.status()
        ml = st.get("ml") or {}
        mm = st.get("last_metrics") or {}
        lw = st.get("layer_watch") or {}
        hist = "\n".join(
            f"  layer {h.get('layer')}: score {h.get('score')} dev {h.get('deviance')}"
            for h in (lw.get("history") or [])[-4:]) or "  —"
        await self._reply(
            "🤖 <b>Metriche AI</b>\n"
            f"ML PrintGuard: score {ml.get('score', '—')} (pred {ml.get('prediction', '—')}, "
            f"soglia {ml.get('threshold', '—')})\n"
            f"CV spaghetti: severità {mm.get('spaghetti_severity', '—')} "
            f"(baseline {mm.get('spaghetti_baseline', '—')})\n"
            f"LayerWatch: layer {lw.get('last_layer', '—')}, {lw.get('samples', 0)} campioni\n"
            f"{hist}")

    async def _cmd_simple(self, name: str, action, logprefix: str, ok_text: str) -> None:
        try:
            await action()
            await self._reply(ok_text, kind=f"tg_{name}")
        except Exception as e:  # noqa: BLE001
            await self._reply(f"⚠️ Comando {name} rifiutato dalla stampante: {e}")

    async def _cmd_speed(self, text: str) -> None:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await self._reply("Uso: /velocita 80 (valore 50-150)")
            return
        pct = int(parts[1])
        if not 50 <= pct <= 150:
            await self._reply("⚠️ Valore fuori range: usa 50-150")
            return
        try:
            await self.printer_api.set_print_speed(pct)
            await self._reply(f"⚡ Velocità di stampa impostata al <b>{pct}%</b>",
                              kind="tg_speed")
        except Exception as e:  # noqa: BLE001
            await self._reply(f"⚠️ Impostazione velocità fallita: {e}")

    async def _cmd_light(self, text: str) -> None:
        arg = text.split(maxsplit=1)
        want = arg[1].strip().lower() if len(arg) > 1 else ""
        if want not in ("on", "off", "1", "0", "acceso", "spento", "true", "false"):
            await self._reply("Uso: /luce on|off (attuale: "
                              f"{'ON 💡' if self.state.light else 'OFF'})")
            return
        on = want in ("on", "1", "acceso", "true")
        try:
            await self.printer_api.set_light(on)
            await self._reply("💡 Luce interna <b>accesa</b>" if on
                              else "🌑 Luce interna <b>spenta</b>", kind="tg_light")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "rifiutato dal firmware" in msg or "occupata" in msg:
                await self._reply(
                    "⚠️ <b>Il firmware della Centauri rifiota i comandi luce "
                    "durante la stampa.</b>\n"
                    "Opzioni: accendila dal display della stampante, oppure "
                    "avvia le stampe da qui (/stampa): la luce viene accesa "
                    "PRIMA del via e resta accesa per tutta la stampa.",
                    kind="tg_light_denied")
            else:
                await self._reply(f"⚠️ Comando luce fallito: {e}")

    async def _cmd_link(self) -> None:
        base = self.cfg.service.get("public_url") or "http://192.168.1.50:8766"
        await self._reply(
            "🔗 <b>Link diretti</b>\n"
            f"🖥 <a href=\"{base}\">Dashboard web</a> — stato, temperature, comandi\n"
            f"📹 <a href=\"{base}/video\">Stream webcam</a> (MJPEG)\n"
            f"🖼 <a href=\"{base}/photo\">Snapshot webcam</a>\n"
            f"📊 <a href=\"{base}/ai/metrics\">Metriche AI</a> (JSON)\n"
            "💡 I link funzionano da rete LAN o VPN (es. Meshnet).",
            kind="tg_link")

    async def _cmd_stop(self, text: str) -> None:
        if "conferma" in text.lower():
            pend = self._pending.pop("stop", None)
            if not pend:
                await self._reply("Nessuno stop in attesa di conferma. "
                                 "Usa prima /stop, poi /stop conferma.")
                return
            if time.time() - pend["ts"] > CONFIRM_TTL:
                await self._reply("Conferma scaduta: ripeti /stop")
                return
            await self._cmd_simple("stop", self.printer_api.stop_print,
                                   "Stop", "🛑 Stampa fermata.")
            return
        self._pending["stop"] = {"ts": time.time()}
        await self._reply(
            "⚠️ <b>Confermi lo STOP della stampa?</b>\n"
            "Rispondi <code>/stop conferma</code> entro 2 minuti per fermarla.")

    async def _cmd_print(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        filename = parts[1].strip().strip('"') if len(parts) > 1 else ""
        if "conferma" in filename.lower():
            pend = self._pending.pop("print", None)
            if not pend:
                await self._reply("Nessuna stampa in attesa. Usa /stampa nomefile.gcode")
                return
            if time.time() - pend["ts"] > CONFIRM_TTL:
                await self._reply("Conferma scaduta: ripeti /stampa " + pend["arg"])
                return
            try:
                ack = await self.printer_api.start_print(pend["arg"])
                if ack == 0:
                    await self._reply(f"▶️ Stampa avviata: {pend['arg']}", kind="tg_print")
                else:
                    await self._reply(f"⚠️ La stampante ha rifiutato (ack {ack}): "
                                      f"file non trovato o occupata.")
            except Exception as e:  # noqa: BLE001
                await self._reply(f"⚠️ Avvio stampa fallito: {e}")
            return
        if not filename:
            await self._reply("Uso: /stampa nomefile.gcode (poi conferma)")
            return
        self._pending["print"] = {"ts": time.time(), "arg": filename}
        await self._reply(
            f"⚠️ <b>Avviare la stampa di {filename}?</b>\n"
            f"Rispondi <code>/stampa {filename} conferma</code> entro 2 minuti.")

    async def _cmd_files(self) -> None:
        lines = ["📁 <b>File sulla stampante</b>"]
        try:
            fl = await self.printer_api.file_list()
            files = [f.get("name", "?") for f in (fl.get("FileList") or [])]
            lines += [f"  {f}" for f in files[:15]] or ["  (nessuno)"]
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ⚠️ non raggiungibile: {e}")
        local = self.uploader.list_local()
        lines.append("📁 <b>File locali (servizio)</b>")
        lines += [f"  {f['name']} ({f['size'] // 1024} KB)" for f in local[:15]] or ["  (nessuno)"]
        await self._reply("\n".join(lines), kind="tg_files")

    # ------------------------------------------------------------------ #
    async def handle_file(self, file_id: str, file_name: str, chat_id) -> dict[str, Any]:
        """GCODE inviato in chat: scarica, salva e trasferisce alla stampante."""
        if not self._authorized(chat_id):
            return {"ok": False, "reason": "chat non autorizzata"}
        name = file_name or "upload.gcode"
        log.info("Upload GCODE da Telegram: %s", name)
        if not name.lower().endswith((".gcode", ".gco", ".g")):
            await self._reply(f"⚠️ {name}: accetto solo file .gcode/.gco")
            return {"ok": False, "reason": "estensione non valida"}
        try:
            await self._reply(f"⏳ Scarico {name} da Telegram…")
            data = await self.telegram.get_file(file_id)
            await self._reply(f"⏳ {len(data) // 1024} KB ricevuti. Salvo e trasferisco "
                              f"alla stampante…")
            result = await self.uploader.upload(name, data, transfer=True)
            await self._reply(
                f"✅ <b>GCODE caricato</b>\n{result['filename']} "
                f"({result['size'] // 1024} KB, md5 ok)\n"
                + ("Trasferito alla stampante 🎉" if result.get("transferred")
                   else "⚠️ Salvato solo in locale (stampante non raggiungibile)")
                + f"\nAvvialo con: /stampa {result['filename']}",
                kind="tg_upload")
            return {"ok": True, "result": result}
        except Exception as e:  # noqa: BLE001
            log.exception("Upload GCODE da Telegram fallito")
            await self._reply(f"❌ Upload fallito: {e}")
            return {"ok": False, "reason": str(e)}
