"""Capability broker — routes goal/proof envelopes between browsers and providers.

Schema: shared/capability.proto (protojson on the wire, data channels labeled
"cap"). One envelope, two verbs:

  {"path": ..., "verb": "goal",  "body": ..., "prev": <hash|"">}
  {"path": ..., "verb": "proof", "body": <evidence>, "prev": <hash|"">}

- Browsers author goals; providers (the ros-bridge, or local resolvers here)
  author proofs. The broker routes goals to the path's provider and fans
  proofs out to that robot's subscribed browsers.
- `prev` chains (hash of the sender's previous wire bytes on the path) give
  supersession order and duplicate/stale rejection — no counters.
- Facts are merged per path from proofs ({"manifest", "value", "citation",
  "tombstone"}); new subscribers get a replay snapshot. Phase-1 note: replayed
  snapshots start fresh chains from this relay; end-to-end provider chains
  come with the store layer.
- The vote resolver is the first *composed* capability: it consumes per-peer
  vote facts plus the connected-streamer set and emits (a) checkable tally
  proofs (citations, not verdicts) and (b) goals to the bridge-provided
  `audio/speaker/enabled` gate.  Rule preserved from the legacy
  broadcast_speaker_state: unanimity of connected streamers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("robot-gateway.capabilities")

GAIN = "audio/gain"
MUTED = "audio/muted"
SPEAKER_ENABLED = "audio/speaker/enabled"
SPEAKER_VOTE = "audio/speaker/vote"

VOTE_MANIFEST = {
    "mode": "PERSISTENT",
    "key": "PEER",
    "schema": {"type": "BOOL"},
    "readOnly": False,
    "description": "Vote to enable the robot speaker (unanimous)",
}


def wire_hash(wire: str) -> str:
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def robot_prefix(robot: str) -> str:
    return f"bot:{robot}/"


def cap_path(robot: str, suffix: str) -> str:
    return f"bot:{robot}/{suffix}"


class _Chain:
    """Per-(path, writer) prev-chain state: head + recently seen hashes."""

    __slots__ = ("head", "seen")

    def __init__(self) -> None:
        self.head: str = ""
        self.seen: deque[str] = deque(maxlen=64)

    def admit(self, msg_hash: str, prev: str) -> bool:
        """True if this message becomes the new head (apply it)."""
        if msg_hash in self.seen:
            return False  # duplicate
        if prev and prev != self.head and prev in self.seen:
            return False  # stale: extends a superseded ancestor
        # prev == head (normal), or unknown prev / chain start (resync): adopt.
        self.head = msg_hash
        self.seen.append(msg_hash)
        return True


class Broker:
    def __init__(
        self,
        get_connected: Callable[[str], set],
        persist_gain: Callable[[str, float], Awaitable[None]],
        load_gain: Callable[[str], Awaitable[float]],
    ) -> None:
        self._get_connected = get_connected
        self._persist_gain = persist_gain
        self._load_gain = load_gain

        # path -> merged fact: {"manifest": ..., "value": ..., "citation": ...}
        self.facts: Dict[str, dict] = {}
        # goal chains keyed by (path, writer); proof chains keyed by path.
        self._goal_chains: Dict[tuple, _Chain] = defaultdict(_Chain)
        self._local_chains: Dict[str, _Chain] = defaultdict(_Chain)

        self._hop1: Dict[str, Any] = {}                    # robot -> bridge cap channel
        self._hop2: Dict[str, list] = defaultdict(list)    # robot -> browser cap channels
        self._votes: Dict[str, Dict[str, bool]] = defaultdict(dict)  # robot -> writer -> True

    # ── hop1 (bridge) ────────────────────────────────────────────────

    def hop1_open(self, robot: str, channel: Any) -> None:
        self._hop1[robot] = channel

        @channel.on("message")
        def _on_message(message: str) -> None:
            try:
                self.handle_bridge_message(robot, message)
            except Exception:
                logger.exception("cap: bad bridge message for %s", robot)

        @channel.on("open")
        def _on_open() -> None:
            logger.info("cap: hop1 channel open for %s", robot)
            asyncio.ensure_future(self._seed_from_db(robot))

        @channel.on("close")
        def _on_close() -> None:
            if self._hop1.get(robot) is channel:
                self._hop1.pop(robot, None)

    def hop1_closed(self, robot: str) -> None:
        self._hop1.pop(robot, None)

    async def _seed_from_db(self, robot: str) -> None:
        """Re-seed the bridge with the durable confirmed gain (server goal)."""
        try:
            gain = await self._load_gain(robot)
        except Exception:
            logger.exception("cap: failed to load gain for %s", robot)
            return
        if gain is not None:
            self.send_goal_to_bridge(robot, cap_path(robot, GAIN), gain)

    def handle_bridge_message(self, robot: str, wire: str) -> None:
        msg = json.loads(wire)
        if msg.get("verb") != "proof":
            return
        path = msg.get("path") or ""
        if not path.startswith(robot_prefix(robot)):
            logger.warning("cap: bridge proof for foreign path %s (robot %s)", path, robot)
            return
        evidence = msg.get("body") or {}
        self._merge_fact(path, evidence)
        # Durable actual: only refl-confirmed values reach the DB.
        if path == cap_path(robot, GAIN) and isinstance(evidence.get("value"), (int, float)):
            asyncio.ensure_future(self._persist_gain(robot, float(evidence["value"])))
        self._fan_out(robot, wire)
        # First declaration of the speaker gate derives the vote capability
        # (Horn rule: vote(X) ⊢ speaker/enabled(X)).
        if path == cap_path(robot, SPEAKER_ENABLED) and "manifest" in evidence:
            self._declare_vote_capability(robot)

    # ── hop2 (browsers) ──────────────────────────────────────────────

    def browser_open(self, robot: str, writer: str, channel: Any) -> None:
        self._hop2[robot].append(channel)

        def _replay() -> None:
            for path, fact in sorted(self.facts.items()):
                if path.startswith(robot_prefix(robot)):
                    self._send(channel, self._local_proof(path, dict(fact)))

        @channel.on("message")
        def _on_message(message: str) -> None:
            try:
                self.handle_browser_message(robot, writer, message)
            except Exception:
                logger.exception("cap: bad browser message from %s", writer)

        @channel.on("open")
        def _on_open() -> None:
            _replay()

        @channel.on("close")
        def _on_close() -> None:
            self.browser_closed(robot, writer, channel)

        if getattr(channel, "readyState", None) == "open":
            _replay()

    def browser_closed(self, robot: str, writer: str, channel: Any) -> None:
        if channel in self._hop2.get(robot, []):
            self._hop2[robot].remove(channel)
        # Connection-scoped facts retract on close.
        if self._votes[robot].pop(writer, None) is not None:
            self._emit_local(robot, cap_path(robot, f"{SPEAKER_VOTE}s/{writer}"),
                             {"tombstone": True})
        self._resolve_speaker(robot)

    def streamers_changed(self, robot: str) -> None:
        """Connected-streamer set changed; the unanimity rule may flip."""
        self._resolve_speaker(robot)

    def handle_browser_message(self, robot: str, writer: str, wire: str) -> None:
        msg = json.loads(wire)
        if msg.get("verb") != "goal":
            return
        path = msg.get("path") or ""
        if not path.startswith(robot_prefix(robot)):
            logger.warning("cap: goal for foreign path %s from %s", path, writer)
            return
        manifest = (self.facts.get(path) or {}).get("manifest")
        if not manifest or manifest.get("readOnly"):
            logger.info("cap: dropping goal for %s (no writable manifest)", path)
            return
        if not self._goal_chains[(path, writer)].admit(wire_hash(wire), msg.get("prev") or ""):
            return  # duplicate or superseded
        if path == cap_path(robot, SPEAKER_VOTE):
            self._handle_vote_goal(robot, writer, msg.get("body"))
        else:
            channel = self._hop1.get(robot)
            if channel is not None:
                self._send(channel, wire)
            else:
                logger.info("cap: no provider online for %s; goal dropped", path)

    # ── vote resolver (first composed capability) ────────────────────

    def _declare_vote_capability(self, robot: str) -> None:
        path = cap_path(robot, SPEAKER_VOTE)
        if "manifest" not in (self.facts.get(path) or {}):
            self._emit_local(robot, path, {"manifest": VOTE_MANIFEST, "value": None})
        self._resolve_speaker(robot)

    def _handle_vote_goal(self, robot: str, writer: str, body: Any) -> None:
        if body:
            self._votes[robot][writer] = True
        else:
            self._votes[robot].pop(writer, None)
        witness = cap_path(robot, f"{SPEAKER_VOTE}s/{writer}")
        self._emit_local(robot, witness,
                         {"value": True} if body else {"tombstone": True})
        self._resolve_speaker(robot)

    def _resolve_speaker(self, robot: str) -> None:
        vote_path = cap_path(robot, SPEAKER_VOTE)
        if "manifest" not in (self.facts.get(vote_path) or {}):
            return  # capability not derived (no speaker gate declared)
        connected = set(self._get_connected(robot))
        enabled = bool(connected) and connected <= set(self._votes[robot].keys())
        citation = {
            "votes": sorted(self._votes[robot].keys()),
            "connected": sorted(connected),
            "rule": "unanimous",
        }
        self._emit_local(robot, vote_path, {"value": enabled, "citation": citation})
        enabled_path = cap_path(robot, SPEAKER_ENABLED)
        current = (self.facts.get(enabled_path) or {}).get("value")
        if current is not None and bool(current) != enabled:
            self.send_goal_to_bridge(robot, enabled_path, enabled)

    # ── local emission / transport ───────────────────────────────────

    def send_goal_to_bridge(self, robot: str, path: str, body: Any) -> None:
        channel = self._hop1.get(robot)
        if channel is None:
            logger.info("cap: no bridge channel for %s; server goal dropped", path)
            return
        chain = self._goal_chains[(path, "server")]
        wire = json.dumps({"path": path, "verb": "goal", "body": body,
                           "prev": chain.head})
        chain.admit(wire_hash(wire), chain.head)
        self._send(channel, wire)

    def _emit_local(self, robot: str, path: str, evidence: dict) -> None:
        self._merge_fact(path, evidence)
        self._fan_out(robot, self._local_proof(path, evidence))

    def _local_proof(self, path: str, evidence: dict) -> str:
        chain = self._local_chains[path]
        wire = json.dumps({"path": path, "verb": "proof", "body": evidence,
                           "prev": chain.head})
        chain.admit(wire_hash(wire), chain.head)
        return wire

    def _merge_fact(self, path: str, evidence: dict) -> None:
        if evidence.get("tombstone"):
            self.facts.pop(path, None)
            return
        fact = self.facts.setdefault(path, {})
        for key in ("manifest", "value", "citation", "provenance"):
            if key in evidence:
                fact[key] = evidence[key]

    def _fan_out(self, robot: str, wire: str) -> None:
        for channel in list(self._hop2.get(robot, [])):
            self._send(channel, wire)

    @staticmethod
    def _send(channel: Any, wire: str) -> None:
        try:
            if getattr(channel, "readyState", None) == "open":
                channel.send(wire)
        except Exception as exc:  # noqa: BLE001 — never let one peer break fan-out
            logger.warning("cap: send failed: %s", exc)
