"""Hermes Git plugin for TotalReclaw.

Installable via ``hermes plugins install graphprotocol/totalreclaw-hermes``.

The heavy lifting — tool handlers, pair-server crypto, HTTP pairing
server, agent hooks — lives in the ``totalreclaw`` Python package on
PyPI. This Git plugin is a thin wrapper that makes TotalReclaw
discoverable to Hermes via its Git-plugin system (``plugin.yaml`` +
``__init__.py`` at repo root).

On load it makes sure the ``totalreclaw`` package is importable:

1. It installs into the environment this process actually imports from.
   Hermes 0.22+ runs on its own Python but imports its dependencies from
   a separate managed venv, so ``sys.executable`` alone is the wrong
   target. Dependencies are installed too.
2. It installs the newest stable release only, never a pre-release.
3. It verifies the import afterwards. If the package still can't be
   loaded, the plugin loads in a degraded mode: a ``totalreclaw_status``
   tool reports the problem and the fix, and the agent is told on the
   first turn that memory is offline, so the user hears about it.

It also checks that ``~/.totalreclaw/`` is writable (Docker volume mounts
created by root) and mirrors the package's SKILL.md into the Hermes skills
directory. Each step is idempotent.

See:
- https://github.com/graphprotocol/totalreclaw
- https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/hermes-setup.md
"""
from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PIP_TIMEOUT_S = 900
# Set when the package could not be made importable; register() then
# degrades instead of failing the whole plugin load.
_LOAD_ERROR: Optional[str] = None
_FIX_COMMAND: Optional[str] = None

_DEGRADED_STATUS_SCHEMA = {
    "name": "totalreclaw_status",
    "description": (
        "Check TotalReclaw billing status, usage, and tier information. "
        "DEGRADED: the totalreclaw Python package could not be loaded, so "
        "only this diagnostic tool is available."
    ),
    "parameters": {"type": "object", "properties": {}},
}

_DEGRADED_NOTICE = (
    "TotalReclaw memory is OFFLINE on this Hermes install: the totalreclaw "
    "package could not be loaded, so nothing is being remembered or "
    "recalled. Tell the user this in your reply, in one sentence, and "
    "offer to show the fix (call totalreclaw_status for the exact command)."
)


def _target_python() -> str:
    """Interpreter of the environment this process imports packages from.

    The first site-packages entry on ``sys.path`` is the one in use. When it
    belongs to a venv (Hermes 0.22+'s managed venv, or a classic Hermes
    venv), target that venv's interpreter; otherwise the running one.
    """
    for entry in sys.path:
        sp = Path(entry)
        if sp.name not in ("site-packages", "dist-packages") or not sp.is_dir():
            continue
        for root in (sp.parent.parent.parent, sp.parent.parent):
            if (root / "pyvenv.cfg").is_file():
                if root.resolve() == Path(sys.prefix).resolve():
                    return sys.executable
                for exe in (root / "bin" / "python3", root / "bin" / "python",
                            root / "Scripts" / "python.exe"):
                    if exe.exists():
                        return str(exe)
        break
    return sys.executable


def _clean_env() -> dict:
    env = dict(os.environ)
    # PYTHONPATH would make pip treat packages from other environments as
    # installed and skip them. The PIP_* overrides could add --pre,
    # --no-deps or a --user/--target install.
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_PRE", "PIP_NO_DEPS",
                "PIP_USER", "PIP_TARGET", "PIP_PREFIX"):
        env.pop(key, None)
    return env


def _has_pip(python: str, env: dict) -> bool:
    try:
        return subprocess.run(
            [python, "-m", "pip", "--version"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=60, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _pip_install_totalreclaw() -> Optional[str]:
    """Install the newest stable ``totalreclaw`` with its dependencies into
    the environment this process imports from. Returns ``None`` on success,
    an error string on failure. Never raises."""
    global _FIX_COMMAND
    env = _clean_env()
    target = _target_python()
    tail = [
        "install", "--disable-pip-version-check", "--no-warn-script-location",
        "--index-url", "https://pypi.org/simple/", "totalreclaw",
    ]
    if target == sys.executable:
        cmd = [sys.executable, "-m", "pip", *tail]
    else:
        cmd = [sys.executable, "-m", "pip", "--python", target, *tail]
    _FIX_COMMAND = " ".join(cmd)
    try:
        if not _has_pip(sys.executable, env):
            # e.g. a venv created with ``uv venv`` / ``--without-pip``.
            if not _has_pip(target, env):
                subprocess.run(
                    [target, "-m", "ensurepip", "--upgrade"], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=300, check=True,
                )
            cmd = [target, "-m", "pip", *tail]
            _FIX_COMMAND = " ".join(cmd)
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, env=env, timeout=_PIP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"`{_FIX_COMMAND}` did not finish within {_PIP_TIMEOUT_S}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run pip: {exc}"
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-2000:]
        return f"`{_FIX_COMMAND}` exited {proc.returncode}. stderr tail:\n{stderr_tail}"
    return None


def _install_totalreclaw_in_venv() -> Optional[str]:
    """Make ``totalreclaw`` importable by this process. Returns ``None`` when
    it is, else a description of what failed."""
    try:
        import totalreclaw.hermes  # noqa: F401
        return None
    except ImportError as exc:
        first_err = exc
    heal_err = _pip_install_totalreclaw()
    if heal_err is not None:
        return f"`import totalreclaw` failed ({first_err}) and the install failed: {heal_err}"
    importlib.invalidate_caches()
    try:
        import totalreclaw.hermes  # noqa: F401,F811
    except ImportError as exc:
        return (
            f"the install ran (`{_FIX_COMMAND}`) but `import totalreclaw` "
            f"still fails: {exc}"
        )
    logger.warning("TotalReclaw: installed the totalreclaw package (%s)", _FIX_COMMAND)
    return None


def _ensure_state_dir_writable() -> None:
    """Ensure ``~/.totalreclaw`` is writable by the current process user.

    On Docker with volume mounts the directory is sometimes created owned
    by root (because the container's first writer was root) while Hermes
    runs as a non-root uid. This raises ``PermissionError`` on the first
    ``pair-sessions.json.lock`` create, long after the plugin loads.

    We surface it at plugin load with a message that tells the user
    exactly what to chown."""
    state = Path.home() / ".totalreclaw"
    try:
        state.mkdir(mode=0o755, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(
            f"~/.totalreclaw ({state}) is not writable by the current "
            f"process user (uid={os.geteuid()}). Fix: from the HOST run "
            f"`docker exec <hermes-container> chown -R $(id -u):$(id -g) {state}`, "
            f"OR recreate the volume mount owned by the container uid."
        ) from exc

    probe = state / ".write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except (PermissionError, OSError) as exc:
        raise RuntimeError(
            f"~/.totalreclaw exists ({state}) but the current process "
            f"cannot write. uid={os.geteuid()}; inspect with `ls -la {state}`. "
            f"Chown the directory to the Hermes runtime user."
        ) from exc


def _install_skill() -> None:
    """Copy the authoritative SKILL.md from the installed ``totalreclaw``
    Python package into the Hermes skills directory so it loads into agent
    context on every turn.

    Source-of-truth design (2026-05-28): the SKILL.md lives ONLY in the
    Python package at ``totalreclaw/hermes/SKILL.md`` (shipped via
    ``[tool.setuptools.package-data]`` in the wheel). This plugin repo
    no longer carries its own SKILL.md — that file historically drifted
    from the canonical version (5 RC cycles of tier copy, recall
    behaviour, restart-branch table, phrase-safety hardening all landed
    in the python package but never reached Hermes' skill loader). See
    graphprotocol/totalreclaw-internal#335 for full context.

    Destination ``<HERMES_HOME>/skills/memory/totalreclaw/SKILL.md`` —
    the ``memory`` category matches upstream Hermes' bundled memory-
    provider conventions (Honcho/Byterover/OpenViking).

    Cleanup: removes the legacy
    ``<HERMES_HOME>/skills/devops/totalreclaw-memory/`` path if present
    (Hermes-the-agent improvised that location in a 2026-05-26 commit
    on this repo's ``feat/skill-md`` branch before the canonical
    mechanism existed; ``rm -rf`` only that exact subdir).

    Idempotent — skips write if destination content matches source.
    Non-fatal: any OSError is logged + swallowed so plugin load never
    fails on a SKILL.md write hiccup.
    """
    try:
        from importlib.resources import files as _resource_files
    except ImportError:  # pragma: no cover — Python <3.9
        logger.debug("importlib.resources.files unavailable — skipping skill install")
        return

    try:
        src_text = _resource_files("totalreclaw.hermes").joinpath("SKILL.md").read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
        # `totalreclaw` not installed yet, or its SKILL.md package-data missing.
        # Bootstrap order ensures the wheel is installed before this runs (see
        # _install_totalreclaw_in_venv above) — if we still can't read SKILL.md,
        # log + skip rather than crashing plugin load.
        logger.debug("Could not read totalreclaw.hermes/SKILL.md from package: %s", exc)
        return

    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        logger.debug("hermes_constants not available — skipping skill install")
        return

    hermes_home = get_hermes_home()

    # Cleanup legacy path. The agent-improvised location was
    # ``skills/devops/totalreclaw-memory/``. Remove only that exact subdir
    # if it exists, leave neighbouring skills alone.
    legacy_dir = hermes_home / "skills" / "devops" / "totalreclaw-memory"
    if legacy_dir.exists():
        try:
            import shutil
            shutil.rmtree(legacy_dir)
            logger.info("Removed legacy TotalReclaw skill location %s", legacy_dir)
        except OSError as exc:
            logger.debug("Could not remove legacy skill dir %s: %s", legacy_dir, exc)
        # Don't bail on cleanup failure — proceed to install the new location.

    skills_dir = hermes_home / "skills" / "memory" / "totalreclaw"
    dst = skills_dir / "SKILL.md"

    if dst.exists() and dst.read_text(encoding="utf-8") == src_text:
        return  # already up to date

    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        dst.write_text(src_text, encoding="utf-8")
        logger.info("Installed TotalReclaw skill to %s", dst)
    except OSError:
        logger.debug("Could not write skill to %s — non-critical", dst, exc_info=True)


def _bootstrap() -> None:
    global _LOAD_ERROR
    _LOAD_ERROR = _install_totalreclaw_in_venv()
    if _LOAD_ERROR is not None:
        logger.error(
            "TotalReclaw loaded DEGRADED — the totalreclaw package could not be "
            "loaded. Only a diagnostic totalreclaw_status tool will be "
            "registered. %s", _LOAD_ERROR,
        )
        return
    _ensure_state_dir_writable()
    _install_skill()


_bootstrap()


def _register_degraded(ctx) -> None:
    """Make a failed bootstrap visible: a status tool that names the problem
    and the fix, plus a first-turn notice so the agent tells the user."""

    def _degraded_status(args, **kw):
        return {
            "ok": False,
            "degraded": True,
            "error": (
                "The totalreclaw Python package could not be loaded in this "
                "Hermes install. Memory tools are unavailable until it is "
                "reinstalled."
            ),
            "fix": (
                "Run: " + (_FIX_COMMAND or (sys.executable + " -m pip install totalreclaw"))
                + "   then restart the Hermes gateway."
            ),
            "detail": _LOAD_ERROR,
        }

    def _degraded_notice(**kw):
        if kw.get("is_first_turn", True):
            return {"context": _DEGRADED_NOTICE}
        return None

    try:
        ctx.register_tool(
            name="totalreclaw_status",
            toolset="totalreclaw",
            schema=_DEGRADED_STATUS_SCHEMA,
            handler=_degraded_status,
        )
    except Exception:
        logger.exception("TotalReclaw: degraded status tool registration failed")
    try:
        ctx.register_hook("pre_llm_call", _degraded_notice)
    except Exception:
        logger.exception("TotalReclaw: degraded notice hook registration failed")


def register(ctx):
    """Hermes calls this on plugin load.

    Delegates to ``totalreclaw.hermes.register`` which wires all
    ``totalreclaw_*`` tools (remember, recall, pair, pin, unpin, status,
    upgrade, debrief, import_from, import_batch, forget, export) plus
    the four agent-lifecycle hooks (on_session_start, pre_llm_call,
    post_llm_call, on_session_end) into the provided ``ctx``.

    The RC-gated ``totalreclaw_report_qa_bug`` tool is registered too
    when the installed ``totalreclaw`` wheel is a pre-release (PEP-440
    ``rcN`` or SemVer ``-rc.N``) build.
    """
    if _LOAD_ERROR is not None:
        return _register_degraded(ctx)
    from totalreclaw.hermes import register as _register

    logger.info("totalreclaw Git plugin loading — delegating to totalreclaw.hermes.register")
    return _register(ctx)
