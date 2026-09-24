#!/usr/bin/env python3
"""Test di accettazione #6 — modelli 3D & slicing on-the-go.

Due livelli:
  A) PIPELINE (sempre, senza PrusaSlicer): upload modelli REST, elenco,
     servizio file, job slicing con uno STUB di slicer (script finto che
     produce un gcode) → coda, stati, log, risultato.
  B) SLICER REALE (solo se prusa-slicer è installato): slice di un cubo
     10mm con i profili Centauri Carbon → gcode prodotto e non vuoto.

Uso:  python3 tests/run_slicer_tests.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from app.config import load_config  # noqa: E402
from app.main import AppContext  # noqa: E402
from app.api.server import create_app  # noqa: E402
from tests.simulator import Simulator  # noqa: E402

SIM_PORT = 13034
API_PORT = 18768
API = f"http://127.0.0.1:{API_PORT}"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    results.append((name, bool(cond), extra))
    line = f"  [{'PASS' if cond else 'FAIL'}] {name}"
    if not cond and extra:
        line += f"  -> {extra}"
    print(line, flush=True)


async def wait_until(cond, timeout: float = 20.0, desc: str = "condizione") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.2)
    raise TimeoutError(desc)


# cubo 10mm in ASCII STL (asset di test, ~1 KB)
CUBE_STL = """solid cube
facet normal 0 0 1
  outer loop
    vertex 0 0 10
    vertex 10 0 10
    vertex 10 10 10
  endloop
endfacet
facet normal 0 0 1
  outer loop
    vertex 0 0 10
    vertex 10 10 10
    vertex 0 10 10
  endloop
endfacet
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 0 10 0
    vertex 10 10 0
  endloop
endfacet
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 10 10 0
    vertex 10 0 0
  endloop
endfacet
endsolid cube
"""


def make_config(slicer_path: str, stub: bool):
    env = {"TELEGRAM_TOKEN": "TEST:TOKEN", "TELEGRAM_CHAT_ID": "12345",
           "TELEGRAM_DRYRUN": "1"}
    cfg = load_config(environ=env)
    cfg.printer.ip = "127.0.0.1"
    cfg.printer.ws_urls = [f"ws://127.0.0.1:{SIM_PORT}/websocket"]
    cfg.printer.http_port = SIM_PORT
    cfg.printer.status_poll_seconds = 1
    cfg.webcam.mjpeg_url = f"http://127.0.0.1:{SIM_PORT}/video"
    cfg.webcam.save_snapshots = False
    cfg.telegram.min_seconds_between_msgs = 0
    cfg.ai.enabled = False
    cfg.mqtt.enabled = False
    cfg.service.port = API_PORT
    cfg.slicer.path = slicer_path
    cfg.slicer.timeout_seconds = 60
    cfg.paths.logs = str(ROOT / "tests/out/logs")
    cfg.paths.snapshots = str(ROOT / "tests/out/snapshots")
    cfg.paths.gcodes = str(ROOT / "tests/out/gcodes")
    cfg.paths.models = str(ROOT / "tests/out/models")
    return cfg


async def pipeline_suite(ctx, session, sim, stub_path: Path):
    """A) upload → list → get → slice (stub) → job done → transfer"""
    form = aiohttp.FormData()
    form.add_field("file", CUBE_STL.encode(), filename="test_cube.stl",
                   content_type="model/stl")
    async with session.post(f"{API}/models", data=form) as r:
        up = await r.json()
    check("Upload modello STL accettato",
          r.status == 200 and up.get("ok") is True, f"{up}")
    check("Modello rifiutato se estensione sbagliata (400)",
          (await session.post(f"{API}/models", data={"file": "x.txt"},
                             )).status in (400, 422))
    async with session.get(f"{API}/models") as r:
        lst = await r.json()
    check("Elenco modelli contiene il caricato",
          any(m["name"] == "test_cube.stl" for m in lst.get("models", [])))
    async with session.get(f"{API}/models/test_cube.stl") as r:
        body = await r.read()
    check("GET modello serve il file STL (model/stl)",
          r.status == 200 and b"vertex 0 0 10" in body
          and "model/stl" in r.headers.get("content-type", ""))
    check("GET modello inesistente → 404",
          (await session.get(f"{API}/models/nope.stl")).status == 404)

    # profili disponibili
    async with session.get(f"{API}/slice/profiles") as r:
        pr = await r.json()
    check("Elenco profili di stampa (≥6 preset ufficiali)",
          r.status == 200 and len(pr.get("profiles", [])) >= 6
          and any(p["id"] == "standard" for p in pr["profiles"]), f"{pr.get('profiles', [])[:2]}")

    # profilo invalido rifiutato
    async with session.post(f"{API}/models/test_cube.stl/slice",
                            json={"profile": "voodoo"}) as r:
        check("Profilo invalido rifiutato (400)",
              r.status == 400, f"status={r.status}")

    # slice con lo stub + profilo
    async with session.post(f"{API}/models/test_cube.stl/slice",
                            json={"profile": "strength", "material": "pla",
                                  "infill": 25, "supports": False,
                                  "transfer": True}) as r:
        sj = await r.json()
    check("Job slicing creato", r.status == 200 and sj.get("job", {}).get("state") in
          ("queued", "running"), f"{sj}")
    job_id = sj["job"]["id"]
    try:
        await wait_until(lambda: ctx.slicer.job(job_id) and
                         ctx.slicer.job(job_id).state in ("done", "error"),
                         timeout=30, desc="job slicing")
    except TimeoutError:
        pass
    async with session.get(f"{API}/slice/jobs/{job_id}") as r:
        job = await r.json()
    check("Job slicing completato con GCODE", job.get("state") == "done"
          and job.get("gcode") == "test_cube.gcode",
          f"state={job.get('state')} err={job.get('error')}")
    gcode = Path(cfg.paths.gcodes) / "test_cube.gcode"
    check("File GCODE prodotto sul volume",
          gcode.is_file() and gcode.read_bytes())
    check("Trasferimento alla stampante eseguito (upload MD5 sim)",
          any(u.get("name") == "test_cube.gcode" for u in sim.uploads))
    check("Parametri invalidi rifiutati (400 material)",
          (await session.post(f"{API}/models/test_cube.stl/slice",
                              json={"material": "abs"})).status == 400)
    check("Job riporta il profilo usato",
          ctx.slicer.job(job_id) is not None
          and ctx.slicer.job(job_id).profile == "strength")
    # cleanup modello
    async with session.delete(f"{API}/models/test_cube.stl") as r:
        await r.json()
    check("DELETE modello", (await session.get(f"{API}/models")).status == 200)


async def real_slice_suite(ctx, session):
    """B) slice REALE con PrusaSlicer + profili Centauri"""
    form = aiohttp.FormData()
    form.add_field("file", CUBE_STL.encode(), filename="real_cube.stl",
                   content_type="model/stl")
    async with session.post(f"{API}/models", data=form) as r:
        await r.json()
    async with session.post(f"{API}/models/real_cube.stl/slice",
                            json={"material": "petg", "layer_height": 0.28,
                                  "infill": 20, "transfer": False}) as r:
        sj = await r.json()
    job_id = sj["job"]["id"]
    try:
        await wait_until(lambda: ctx.slicer.job(job_id) and
                         ctx.slicer.job(job_id).state in ("done", "error"),
                         timeout=300, desc="slice reale")
    except TimeoutError:
        pass
    job = ctx.slicer.job(job_id)
    check("Slice REALE: gcode prodotto",
          job.state == "done" and job.gcode == "real_cube.gcode",
          f"state={job.state} err={job.error or ''} log={job.log_tail[-200:]}")
    if job.state == "done":
        gcode = (Path(ctx.cfg.paths.gcodes) / job.gcode).read_text()
        check("GCODE reale: contiene G1/G28 e non è vuoto",
              ("G1" in gcode or "G28" in gcode) and len(gcode) > 500,
              f"{len(gcode)} bytes")


async def main() -> int:
    print("== Test modelli 3D & slicing ==", flush=True)
    global cfg
    # stub slicer: copia un gcode fittizio dove prusa-slicer scriverebbe
    stub = ROOT / "tests/out/fake_slicer.py"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        '#!/usr/bin/env python3' + chr(10)
        + 'import sys, pathlib' + chr(10)
        + 'args = sys.argv[1:]' + chr(10)
        + 'if "--help" in args:' + chr(10)
        + '    sys.exit(0)' + chr(10)
        + 'out = pathlib.Path(args[args.index("-o") + 1]) if "-o" in args else None' + chr(10)
        + 'if out:' + chr(10)
        + '    out.write_text("; FAKED GCODE" + chr(10) + "G28" + chr(10) + "G1 X10" + chr(10))' + chr(10)
        + 'print("faked slicing done")' + chr(10))
    stub.chmod(0o755)

    sim = Simulator(port=SIM_PORT)
    runner = web.AppRunner(sim.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", SIM_PORT).start()

    # A) pipeline con STUB
    cfg = make_config(str(stub), stub=True)
    ctx = AppContext(cfg)
    await ctx.start()
    import uvicorn
    uv = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1",
                                        port=API_PORT, log_level="error"))
    uv_task = asyncio.create_task(uv.serve())
    await asyncio.sleep(1.0)
    session = aiohttp.ClientSession()
    try:
        await pipeline_suite(ctx, session, sim, stub)
    finally:
        await session.close()
        uv_task.cancel()
        try:
            await uv_task
        except (asyncio.CancelledError, Exception):
            pass
        await ctx.stop()

    # B) slice reale se prusa-slicer è installato (stesso contesto, nuovo ctx)
    real = await _has_prusa()
    if real:
        await runner.cleanup()
        sim2 = Simulator(port=SIM_PORT)
        runner = web.AppRunner(sim2.build_app())
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", SIM_PORT).start()
        cfg = make_config("prusa-slicer", stub=False)
        ctx = AppContext(cfg)
        await ctx.start()
        uv = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1",
                                            port=API_PORT, log_level="error"))
        uv_task = asyncio.create_task(uv.serve())
        await asyncio.sleep(1.0)
        session = aiohttp.ClientSession()
        try:
            await real_slice_suite(ctx, session)
        finally:
            await session.close()
            uv_task.cancel()
            try:
                await uv_task
            except (asyncio.CancelledError, Exception):
                pass
            await ctx.stop()
    else:
        print("  [SKIP] prusa-slicer non installato: slice reale non testata")
    await runner.cleanup()

    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f"\n== RISULTATO: {passed}/{total} PASS ==")
    if passed == total:
        print("TEST SLICER: SUCCESSO")
        return 0
    print("TEST SLICER: FALLITO")
    return 1


async def _has_prusa() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "prusa-slicer", "--help",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
            return proc.returncode == 0
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            return False
    except (FileNotFoundError, OSError):
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
