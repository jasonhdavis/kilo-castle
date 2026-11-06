"""Slack bridge: surface pipeline blockers and carry Slack replies back as decrees.

The bridge is deliberately thin and stdlib-only (``urllib.request``, no SDK):

  * ``court slack post`` lets a Steward push one ad-hoc item to M'Lord.
  * ``court slack sync`` is the deterministic cron entry point: it reads the
    board snapshot plus the Night's Watch state, posts only items whose
    fingerprint is new, then polls the threads it owns for replies.
  * A reply is authored as a normal ``.court/decrees/<id>.json`` packet (the
    same shape ``court decree`` writes) and validated with
    ``decisions.validate_decree``. The bridge NEVER applies a ruling directly:
    the decree packet + pleasance pickup lane remain the only path.

Dedupe/consumed-reply state lives OUTSIDE the git tree, under
``~/.local/state/kilo-castle/slack/<repo-slug>.json`` (``COURT_STATE_DIR``
overrides the base for tests), so repeated syncs never spam a channel and a
reply is never turned into two packets.

Configuration is most-specific first:

  1. env ``COURT_SLACK_TOKEN`` / ``COURT_SLACK_CHANNEL`` /
     ``COURT_SLACK_NOTIFY_USER`` / ``COURT_SLACK_ENV_FILE``;
  2. a ``slack`` block in ``.court/config.json``
     (``{"token_env": "SLACK_BOT_TOKEN", "channel": "", "notify_user": "..."}``);
  3. for the token only, a fallback env-file search for a sibling
     ``pb-app/.env`` above the repo root.

The token is never echoed; every error string passes through ``_redact``.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config
from . import decisions
from . import git_ops
from . import nightwatch
from . import store

SLACK_API = "https://slack.com/api/"
REQUEST_TIMEOUT = 10
STATE_VERSION = 1
DEFAULT_TOKEN_ENV = "SLACK_BOT_TOKEN"
DEFAULT_NOTIFY_USER = "U02MA74K9DY"
MAX_RULING_CHARS = 2000
MAX_INDIVIDUAL_MESSAGES = 5
DIGEST_MAX_LINES = 20

SCOPE_HINT = (
    "chat:write, im:history (DMs), channels:history (public channels), "
    "groups:history (private channels)"
)

_TOKEN_RE = re.compile(r"xox[baprs]-[A-Za-z0-9-]+")


class SlackError(RuntimeError):
    """A Slack call or configuration problem with an actionable message."""


@dataclass
class SlackConfig:
    repo: Path
    token: str = ""
    channel: str = ""
    notify_user: str = ""
    token_env: str = DEFAULT_TOKEN_ENV
    token_source: str = ""
    env_file: str = ""

    @property
    def target(self) -> str:
        return self.channel or self.notify_user

    @property
    def configured(self) -> bool:
        return bool(self.token and self.target)


def _redact(text: str, token: str = "") -> str:
    """Never let a bot token reach stdout, stderr, or a state file."""
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    return _TOKEN_RE.sub("***", out)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Configuration (env > manifest > sibling pb-app/.env, for the token only)
# ---------------------------------------------------------------------------


def _parse_env_file(path: Path, key: str) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        return value.strip().strip('"').strip("'")
    return ""


def _fallback_env_file_token(repo: Path, key: str) -> tuple[str, str]:
    """The token-only fallback: a sibling ``pb-app/.env`` up the tree."""
    root = Path(repo).resolve()
    seen: set[Path] = set()
    for base in (root, *root.parents):
        candidate = base / "pb-app" / ".env"
        if candidate in seen:
            continue
        seen.add(candidate)
        token = _parse_env_file(candidate, key)
        if token:
            return token, str(candidate)
    return "", ""


def resolve_config(repo: Path) -> SlackConfig:
    repo = Path(repo).resolve()
    cfg = config.load_config(repo / ".court")
    block = cfg.get("slack") if isinstance(cfg.get("slack"), dict) else {}
    token_env = str(block.get("token_env") or DEFAULT_TOKEN_ENV).strip() or DEFAULT_TOKEN_ENV
    channel = str(block.get("channel") or "").strip()
    notify_user = str(block.get("notify_user") or "").strip()
    token = ""
    source = ""
    env_file = ""

    for name in ("COURT_SLACK_CHANNEL", "COURT_SLACK_NOTIFY_USER"):
        value = os.environ.get(name)
        if value and value.strip():
            if name == "COURT_SLACK_CHANNEL":
                channel = value.strip()
            else:
                notify_user = value.strip()

    explicit_token = os.environ.get("COURT_SLACK_TOKEN", "").strip()
    if explicit_token:
        token, source = explicit_token, "env COURT_SLACK_TOKEN"
    if not token:
        env_token = os.environ.get(token_env, "").strip()
        if env_token:
            token, source = env_token, f"env {token_env}"
    if not token:
        explicit_file = os.environ.get("COURT_SLACK_ENV_FILE", "").strip()
        if explicit_file:
            found = _parse_env_file(Path(explicit_file), token_env)
            if found:
                token, source, env_file = found, f"{token_env} in env file", explicit_file
    if not token:
        found, path = _fallback_env_file_token(repo, token_env)
        if found:
            token, source, env_file = found, f"{token_env} in sibling pb-app/.env", path

    if not notify_user:
        notify_user = DEFAULT_NOTIFY_USER
    return SlackConfig(
        repo=repo, token=token, channel=channel, notify_user=notify_user,
        token_env=token_env, token_source=source, env_file=env_file,
    )


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------


def _api(cfg: SlackConfig, method: str, payload: Optional[dict] = None) -> dict:
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{SLACK_API}{method}", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {cfg.token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SlackError(_redact(
            f"{method} failed with HTTP {e.code}: {e.reason}", cfg.token))
    except urllib.error.URLError as e:
        raise SlackError(_redact(f"{method} could not reach Slack: {e.reason}", cfg.token))
    except OSError as e:
        raise SlackError(_redact(f"{method} could not reach Slack: {e}", cfg.token))

    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise SlackError(f"{method} returned a non-JSON response")
    if not data.get("ok"):
        error = str(data.get("error") or "unknown_error")
        if error == "missing_scope":
            needed = data.get("needed") or data.get("scopes") or ""
            raise SlackError(_redact(
                f"missing_scope: the bot lacks {needed or 'a required scope'}; "
                f"grant {SCOPE_HINT} and reinstall the app", cfg.token))
        hint = {
            "invalid_auth": "the bot token is invalid or revoked",
            "not_authed": "no bot token was supplied",
            "token_revoked": "the bot token was revoked",
            "channel_not_found": "the channel id does not exist or the bot is not in it",
            "not_in_channel": "invite the bot to the channel first",
            "ratelimited": "Slack is rate-limiting this call; retry in a minute",
            "cannot_dm_bot": "the notify target is a bot, not a person",
        }.get(error, error)
        raise SlackError(_redact(f"{method} failed: {hint}", cfg.token))
    return data


def auth_test(cfg: SlackConfig) -> dict:
    data = _api(cfg, "auth.test")
    return {"team": data.get("team") or "", "user": data.get("user") or "",
            "user_id": data.get("user_id") or "", "url": data.get("url") or ""}


def _looks_like_user(target: str) -> bool:
    return target.startswith("U") or target.startswith("W")


def open_dm(cfg: SlackConfig, user: str, state: Optional[dict] = None) -> str:
    """Resolve (and remember) the DM channel id for a user id."""
    if state:
        cached = (state.get("dm_channels") or {}).get(user)
        if cached:
            return cached
    data = _api(cfg, "conversations.open", {"users": user})
    channel = (data.get("channel") or {}).get("id") or ""
    if not channel:
        raise SlackError(f"conversations.open returned no channel for {user}")
    if state is not None:
        state.setdefault("dm_channels", {})[user] = channel
    return channel


def post_message(cfg: SlackConfig, text: str, state: Optional[dict] = None,
                 target: Optional[str] = None) -> dict:
    """Post plain text to a channel id or DM a user id; return ts + channel."""
    dest = (target or cfg.target or "").strip()
    if not dest:
        raise SlackError("no Slack target configured (set slack.channel or slack.notify_user)")
    channel = open_dm(cfg, dest, state) if _looks_like_user(dest) else dest
    data = _api(cfg, "chat.postMessage", {
        "channel": channel,
        "text": text,
        "mrkdwn": True,
        "unfurl_links": False,
        "unfurl_media": False,
    })
    ts = data.get("ts") or ""
    if not ts:
        raise SlackError("chat.postMessage returned no message timestamp")
    return {"ts": ts, "channel": data.get("channel") or channel, "target": dest}


def conversations_replies(cfg: SlackConfig, channel: str, ts: str) -> dict:
    return _api(cfg, "conversations.replies", {"channel": channel, "ts": ts})


def conversations_history(cfg: SlackConfig, channel: str, limit: int = 20) -> dict:
    return _api(cfg, "conversations.history", {"channel": channel, "limit": limit})


# ---------------------------------------------------------------------------
# Durable bridge state (OUTSIDE the git tree)
# ---------------------------------------------------------------------------


def state_root() -> Path:
    override = os.environ.get("COURT_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "kilo-castle"
    return Path.home() / ".local" / "state" / "kilo-castle"


def state_dir(repo: Path) -> Path:
    return state_root() / "slack"


def state_path(repo: Path) -> Path:
    return state_dir(repo) / f"{nightwatch.repo_slug(repo)}.json"


def load_state(repo: Path) -> dict:
    p = state_path(repo)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("version") == STATE_VERSION:
            payload.setdefault("posted", {})
            payload.setdefault("consumed_replies", [])
            payload.setdefault("dm_channels", {})
            payload.setdefault("last_sync", "")
            return payload
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "repo": str(repo), "posted": {},
            "consumed_replies": [], "dm_channels": {}, "last_sync": ""}


def save_state(repo: Path, state: dict) -> None:
    p = state_path(repo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Fingerprints and the outbound digest
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    return " ".join((text or "").lower().split())


def fingerprint(kind: str, quest_id: str, text: str) -> str:
    seed = "\x00".join((kind or "", quest_id or "", normalize_text(text)))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _item(kind: str, quest_id: str, title: str, text: str) -> dict:
    return {
        "kind": kind,
        "quest": quest_id,
        "title": title,
        "text": text,
        "fingerprint": fingerprint(kind, quest_id, text),
    }


def _watch_budget(watch_state: dict) -> tuple[int, int]:
    """The live watch policy's worker-recovery budget (defaults when absent)."""
    policy = watch_state.get("policy") if isinstance(watch_state.get("policy"), dict) else {}
    try:
        slots = max(1, int(policy.get("serf_slots") or 2))
    except (TypeError, ValueError):
        slots = 2
    try:
        per_serf = max(1, int(policy.get("goads_per_serf") or 3))
    except (TypeError, ValueError):
        per_serf = 3
    return slots, per_serf


def _worker_recovery_spent(qstate: dict, watch_state: dict) -> bool:
    """True once goad/dispatch recovery is exhausted and a human must step in."""
    slots, per_serf = _watch_budget(watch_state)
    try:
        goads = int(qstate.get("goads") or 0)
    except (TypeError, ValueError):
        goads = 0
    try:
        serf_index = int(qstate.get("serf_index") or 0)
    except (TypeError, ValueError):
        serf_index = 0
    return goads >= slots * per_serf or (serf_index + 1) >= slots


def blocker_items(snap: dict, watch_state: dict) -> list[dict]:
    """Only blockers the watch CANNOT clear mechanically.

    Base drift on a WORKING quest is not a notification: the watch rebases it.
    A drift/conflict item is emitted only when the Quest is genuinely parked for
    a human — an escalated ``merge_conflict`` marker, or a TRIBUTE_READY/GATE
    Quest whose last rebase/coin attempt recorded a conflict. A worker-stalled
    item appears only after the goad/dispatch recovery budget is spent.
    """
    items: list[dict] = []
    watch_quests = watch_state.get("quests") or {}

    for quest in (snap.get("quests") or []):
        if quest.get("kind") == "epic":
            continue
        qid = str(quest.get("id") or "")
        if not qid:
            continue
        status = str(quest.get("status") or "").upper()
        git_status = quest.get("git_status") or {}
        behind = git_status.get("behind")
        behind = behind if isinstance(behind, int) and behind > 0 else 0
        qstate = watch_quests.get(qid) if isinstance(watch_quests.get(qid), dict) else {}

        conflict = qstate.get("merge_conflict")
        if isinstance(conflict, dict):
            escalated = bool(conflict.get("escalated_ts"))
            # A fresh conflict on WORKING is still being escalated by the watch;
            # only an escalated marker, or a gate-stage quest whose last
            # rebase/coin attempt conflicted, is a human-parked blocker.
            if escalated or status in ("TRIBUTE_READY", "GATE"):
                files = ", ".join(sorted(str(f) for f in (conflict.get("files") or []))[:8])
                headline = "merge conflict"
                if behind:
                    headline += f" and {behind} commit(s) behind castle"
                text = (
                    f"[BLOCKER] {qid} — {headline} ({status or 'unknown'})\n\n"
                    f"Conflicting file(s): {files or '(unknown)'}\n"
                    + ("The worktree has drifted from the trunk. " if behind else "")
                    + "The rebase is held; a Steward decision is needed."
                )
                items.append(_item("blocker", qid, str(quest.get("title") or ""), text))
                continue

        worker = str((quest.get("worker") or {}).get("state") or "idle")
        if worker == "stalled" and _worker_recovery_spent(qstate, watch_state):
            slots, per_serf = _watch_budget(watch_state)
            text = (
                f"[BLOCKER] {qid} — worker stalled, recovery budget spent "
                f"({status or 'unknown'})\n\n"
                f"The worker is stalled and the watch has spent its goad/dispatch "
                f"budget ({int(qstate.get('goads') or 0)}/{slots * per_serf}); "
                "a Steward must step in."
            )
            items.append(_item("blocker", qid, str(quest.get("title") or ""), text))

    last_pickup = watch_state.get("last_pickup") or {}
    rejected = str(last_pickup.get("rejected") or "").strip()
    remedy = str(
        last_pickup.get("remedy")
        or watch_state.get("pickup_remedy")
        or ""
    ).strip()
    if rejected:
        try:
            streak = int(watch_state.get("pickup_streak") or 1)
        except (TypeError, ValueError):
            streak = 1
        # One rejection is normal churn; the watch retries it. Three in a row
        # (or an explicit remedy) is a standing blocker a human must see.
        if streak >= 3 or remedy:
            text = (
                f"[BLOCKER] Pleasance pickup rejected ({streak} consecutive)\n\n"
                f"{rejected}\n"
                + (f"\nRemedy: {remedy}\n" if remedy else "")
                + "\nPaperwork is not reaching castle; reply if this needs a ruling."
            )
            items.append(_item("blocker", "", "Pleasance pickup", text))

    return items


def decision_items(repo: Path, snap: Optional[dict] = None) -> list[dict]:
    items: list[dict] = []
    for decision in decisions.collect(repo, snap=snap):
        qid = str(decision.get("id") or "")
        title = str(decision.get("title") or "")
        text = (
            f"[DECISION] {qid} — {title} "
            f"({decision.get('status') or '?'} · {decision.get('kind') or '?'})\n\n"
            f"Question: {decision.get('question') or ''}\n"
            f"When ruled: {decision.get('next') or ''}\n\n"
            "Reply in this thread with your ruling; the reply becomes a decree packet."
        )
        items.append(_item("decision", qid, title, text))
    return items


def collect_items(repo: Path, snap: dict, watch_state: dict) -> list[dict]:
    return blocker_items(snap, watch_state) + decision_items(repo, snap=snap)


# ---------------------------------------------------------------------------
# Reply ingestion: Slack text -> a validated royal decree packet
# ---------------------------------------------------------------------------


_MENTION_RE = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
_CHANNEL_RE = re.compile(r"<#[A-Z0-9]+(?:\|[^>]*)?>")
_SPECIAL_RE = re.compile(r"<!(?:here|channel|everyone)(?:\|[^>]*)?>")
_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")


def strip_mrkdwn(text: str) -> str:
    """Minimal Slack mrkdwn cleanup; the ruling itself stays verbatim."""
    out = html.unescape(text or "")
    out = _MENTION_RE.sub("", out)
    out = _CHANNEL_RE.sub("", out)
    out = _SPECIAL_RE.sub("", out)
    out = _LINK_RE.sub(lambda m: m.group(2) or m.group(1), out)
    out = " ".join(out.split())
    return out[:MAX_RULING_CHARS].strip()


def _decree_id(reply_ts: str, quest_id: str) -> str:
    digest = hashlib.sha1(f"{reply_ts}:{quest_id}".encode("utf-8")).hexdigest()[:6]
    stamp = _now_iso().replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
    return f"decree-slack-{stamp[:15]}-{digest}"


def author_decree(repo: Path, quest_id: str, ruling: str, channel: str,
                  reply_ts: str, dry_run: bool = False, say=print) -> Optional[Path]:
    """Write + validate + (by default) commit one decree packet; never apply it."""
    did = _decree_id(reply_ts, quest_id)
    packet = {
        "id": did,
        "by": "M'Lord",
        "at": _now_iso(),
        "source": f"slack reply in {channel}",
        "decisions": [{
            "quest": quest_id,
            "ruling": ruling[:MAX_RULING_CHARS],
        }],
    }
    text = json.dumps(packet, indent=2) + "\n"
    ok, why = decisions.validate_decree(text, repo)
    if not ok:
        say(f"ERROR: Slack reply for {quest_id} was not a valid decree: {why}")
        return None
    target = decisions.decree_dir(repo) / f"{did}.json"
    if dry_run:
        say(f"  would write {target.relative_to(repo)} (dry run)")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    say(f"  wrote {target.relative_to(repo)}")
    result = git_ops.git_commit_paths([target], f"court: slack decree {did}", cwd=repo)
    if not result.get("ok") and not result.get("no_changes"):
        say(f"  WARNING: decree autocommit failed: {result.get('stderr') or result.get('warning')}")
    say(f"  follow-up: deliver {target.relative_to(repo)} on the pleasance branch "
        f"(pickup validates and applies it), or run "
        f"`court decree --quest {quest_id} --ruling \"...\"` to apply locally now")
    return target


def ingest_replies(cfg: SlackConfig, repo: Path, state: dict,
                   dry_run: bool = False, say=print) -> list[dict]:
    """Turn unconsumed thread replies into decree packets (one per reply)."""
    authored: list[dict] = []
    consumed = set(state.get("consumed_replies") or [])
    for rec in list((state.get("posted") or {}).values()):
        quest_id = str(rec.get("quest") or "")
        channel = str(rec.get("channel") or "")
        anchor = str(rec.get("ts") or "")
        if not (quest_id and channel and anchor):
            continue
        if dry_run:
            continue
        data = conversations_replies(cfg, channel, anchor)
        for message in (data.get("messages") or []):
            ts = str(message.get("ts") or "")
            if not ts or ts == anchor or ts in consumed:
                continue
            if message.get("bot_id") or message.get("subtype") == "bot_message":
                continue
            ruling = strip_mrkdwn(str(message.get("text") or ""))
            if not ruling:
                continue
            path = author_decree(repo, quest_id, ruling, channel, ts,
                                 dry_run=dry_run, say=say)
            if path is None:
                continue
            state.setdefault("consumed_replies", []).append(ts)
            consumed.add(ts)
            authored.append({"quest": quest_id, "ts": ts, "path": str(path),
                             "ruling": ruling})
    return authored


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def _digest_line(item: dict) -> str:
    text = " ".join(str(item.get("text") or "").split())
    text = re.sub(r"^\[(?:BLOCKER|DECISION)\]\s*", "", text)
    return f"- {item.get('quest') or 'pickup'}: {item.get('kind') or 'blocker'} — {text}"[:240]


def _digest_text(overflow: list[dict]) -> str:
    shown = overflow[:DIGEST_MAX_LINES]
    hidden = max(0, len(overflow) - len(shown))
    lines = [
        f"[DIGEST] {len(overflow)} more pipeline item(s) need attention",
        "",
        *[_digest_line(item) for item in shown],
    ]
    if hidden:
        lines.append(f"- …and {hidden} more")
    lines += ["", "Each item above is also recorded; fix or rule on it where it lives."]
    return "\n".join(lines)


def sync(repo: Path, dry_run: bool = False, snap: Optional[dict] = None,
         say=print) -> dict:
    """Post new/changed items, then ingest replies. Never raises on Slack errors."""
    repo = Path(repo).resolve()
    cfg = resolve_config(repo)
    if not cfg.configured:
        reason = "no bot token" if not cfg.token else "no channel or notify user"
        say(f"Slack bridge is not configured ({reason}); set COURT_SLACK_TOKEN "
            f"(or SLACK_BOT_TOKEN) and slack.channel / slack.notify_user. Nothing sent.")
        return {"configured": False, "posted": [], "replies": [], "would_post": []}

    try:
        if snap is None:
            snap = nightwatch.snapshot(repo)
        watch_state = nightwatch.load_state(repo)
    except Exception as e:  # noqa: BLE001 — a broker failure must not crash cron
        say(_redact(f"ERROR: could not read the board snapshot: {e}", cfg.token))
        return {"configured": True, "error": "snapshot_failed", "posted": [], "replies": []}

    state = load_state(repo)
    items = collect_items(repo, snap, watch_state)
    new_items = [item for item in items
                 if item["fingerprint"] not in (state.get("posted") or {})]

    result = {"configured": True, "posted": [], "replies": [], "would_post": []}
    if dry_run:
        result["would_post"] = [
            {"kind": i["kind"], "quest": i["quest"], "text": i["text"]}
            for i in new_items
        ]
        say(f"Slack sync (dry run): {len(new_items)} new item(s) would be posted; "
            "no message, reply, file, or state change was made.")
        return result

    individual = new_items[:MAX_INDIVIDUAL_MESSAGES]
    overflow = new_items[MAX_INDIVIDUAL_MESSAGES:]
    try:
        for item in individual:
            sent = post_message(cfg, item["text"], state=state)
            state.setdefault("posted", {})[item["fingerprint"]] = {
                "ts": sent["ts"],
                "channel": sent["channel"],
                "kind": item["kind"],
                "quest": item["quest"],
                "posted_at": _now_iso(),
            }
            save_state(repo, state)
            result["posted"].append({
                "kind": item["kind"], "quest": item["quest"],
                "ts": sent["ts"], "channel": sent["channel"],
            })
        if overflow:
            # One bounded digest for the remainder, never another message per
            # item. Every overflow fingerprint is recorded so the digest cannot
            # repeat on the next tick; quest is blank so a digest-thread reply is
            # never misattributed to one of the listed quests.
            sent = post_message(cfg, _digest_text(overflow), state=state)
            for item in overflow:
                state.setdefault("posted", {})[item["fingerprint"]] = {
                    "ts": sent["ts"],
                    "channel": sent["channel"],
                    "kind": item["kind"],
                    "quest": "",
                    "posted_at": _now_iso(),
                    "digest": True,
                }
            save_state(repo, state)
            result["posted"].append({
                "kind": "digest", "quest": "",
                "ts": sent["ts"], "channel": sent["channel"],
                "count": len(overflow),
            })
        result["replies"] = ingest_replies(cfg, repo, state, say=say)
    except SlackError as e:
        result["error"] = _redact(str(e), cfg.token)
        say(f"ERROR: {result['error']}")
    except Exception as e:  # noqa: BLE001 — cron entry point stays alive
        result["error"] = _redact(str(e), cfg.token)
        say(f"ERROR: {result['error']}")

    state["last_sync"] = _now_iso()
    save_state(repo, state)
    say(f"Slack sync: {len(result['posted'])} new item(s) posted, "
        f"{len(result['replies'])} reply/replies consumed.")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def render_status(repo: Path, probe: bool = False) -> str:
    cfg = resolve_config(repo)
    state = load_state(repo)
    lines = [
        f"Slack bridge — {'configured' if cfg.configured else 'not configured'}",
        f"  token: {'present' if cfg.token else 'absent'}"
        + (f" (source: {cfg.token_source})" if cfg.token else ""),
        f"  target: {cfg.target or '(none)'}"
        + (" (direct message)" if cfg.notify_user and not cfg.channel else " (channel)"),
        f"  last sync: {state.get('last_sync') or '(never)'}",
        f"  posted: {len(state.get('posted') or {})} item(s)",
        f"  consumed replies: {len(state.get('consumed_replies') or [])}",
        f"  state: {state_path(repo)}",
    ]
    if not cfg.configured:
        lines.append("  hint: set COURT_SLACK_TOKEN (or SLACK_BOT_TOKEN in the env or a "
                     "sibling pb-app/.env) plus slack.channel / slack.notify_user in "
                     ".court/config.json")
    if probe:
        if not cfg.token:
            lines.append("  probe: skipped (no token)")
        else:
            try:
                who = auth_test(cfg)
                lines.append(f"  probe: team={who['team'] or '?'} user={who['user'] or '?'}")
            except SlackError as e:
                lines.append(f"  probe: FAILED — {_redact(str(e), cfg.token)}")
    return "\n".join(lines)


def cmd_status(args) -> None:
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    print(render_status(repo, probe=bool(getattr(args, "probe", False))))


def cmd_post(args) -> None:
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    cfg = resolve_config(repo)
    if not cfg.configured:
        print("ERROR: Slack bridge is not configured; set COURT_SLACK_TOKEN and a "
              "channel/notify user first.", file=sys.stderr)
        raise SystemExit(1)
    quest_id = str(getattr(args, "quest", "") or "").strip()
    if not quest_id:
        print("ERROR: --quest is required for `court slack post`", file=sys.stderr)
        raise SystemExit(1)
    kind = str(getattr(args, "kind", "") or "note").strip().lower()
    if kind not in ("blocker", "decision", "note"):
        print("ERROR: --kind must be blocker, decision, or note", file=sys.stderr)
        raise SystemExit(1)
    title, status = "", ""
    try:
        quest = store.load(quest_id, court_root=repo / ".court")
        title, status = quest.title, quest.status
    except Exception:  # noqa: BLE001 — an ad-hoc note may name an untracked id
        pass
    body = f"[{kind.upper()}] {quest_id}"
    if title:
        body += f" — {title}"
    if status:
        body += f" ({status})"
    body += f"\n\n{getattr(args, 'text', '') or ''}"
    body += "\n\nReply in this thread with a ruling if this needs a decision."
    state = load_state(repo)
    fp = fingerprint(kind, quest_id, body)
    if fp in (state.get("posted") or {}):
        print(f"Already posted this {kind} for {quest_id}; nothing sent.")
        return
    try:
        sent = post_message(cfg, body, state=state)
    except SlackError as e:
        print(f"ERROR: {_redact(str(e), cfg.token)}", file=sys.stderr)
        raise SystemExit(1)
    state.setdefault("posted", {})[fp] = {
        "ts": sent["ts"], "channel": sent["channel"], "kind": kind,
        "quest": quest_id, "posted_at": _now_iso(),
    }
    save_state(repo, state)
    print(f"Posted {kind} for {quest_id} to {sent['channel']} (ts {sent['ts']}).")


def cmd_sync(args) -> None:
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    as_json = bool(getattr(args, "json", False))
    # --json is a machine surface: suppress every human line so the caller gets
    # exactly one parseable document (dry-run and real runs alike).
    say = (lambda *a, **k: None) if as_json else print
    result = sync(repo, dry_run=bool(getattr(args, "dry_run", False)), say=say)
    if as_json:
        print(json.dumps(result, indent=2))


def cmd_slack(args) -> None:
    action = str(getattr(args, "action", "") or "").strip().lower()
    if action == "status":
        cmd_status(args)
    elif action == "post":
        cmd_post(args)
    elif action == "sync":
        cmd_sync(args)
    else:
        print("ERROR: expected `court slack status|post|sync`", file=sys.stderr)
        raise SystemExit(2)
