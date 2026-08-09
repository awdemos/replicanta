"""Group chat: several organisms sharing one transcript. Each organism
is a full Organism loaded from the nursery; the GroupChat owns the
shared message log and decides who speaks. A user line is broadcast to
every member (or just the one addressed as 'fern: …' / '@fern …'), each
speaker replies through its own thought arena in quick mode (one
generation, not the five-call debate — a full debate per speaker per
line would cost minutes), and every utterance lands in the speaker's
own episodic memory so relationships with the other members persist.

Pure orchestration — no textual imports — so it is unit-testable
without a terminal."""

import narration

MAX_CONTEXT = 8        # recent transcript lines folded into each prompt
MAX_TRANSCRIPT = 200   # hard cap, oldest lines dropped first


class GroupChat:
    def __init__(self, members):
        """members: dict of name -> Organism, in speaking order."""
        if len(members) < 2:
            raise ValueError("a group chat needs at least two organisms")
        self.members = dict(members)
        self.transcript = []          # list of (speaker, text)

    def names(self):
        return list(self.members)

    def _append(self, speaker, text):
        self.transcript.append((speaker, text))
        if len(self.transcript) > MAX_TRANSCRIPT:
            del self.transcript[:len(self.transcript) - MAX_TRANSCRIPT]

    def _addressed(self, text):
        """'fern: hi' or '@fern hi' addresses only fern (when a member)."""
        if text.startswith("@"):
            head = text[1:].split(None, 1)[0].strip().rstrip(":")
            return head if head in self.members else None
        if ":" in text:
            head = text.split(":", 1)[0].strip()
            return head if head in self.members else None
        return None

    def context(self):
        """The prompt fragment every speaker sees: the roster plus the
        most recent transcript lines."""
        roster = ", ".join(self.names())
        recent = "\n".join(f"{speaker}: {text}"
                           for speaker, text in self.transcript[-MAX_CONTEXT:])
        return (f"You are in a group chat with {roster} and the user. "
                "Reply as yourself — one or two short sentences, and "
                "address others by name when you speak to them. "
                f"Recent messages:\n{recent}")

    def broadcast(self, user_text, quick=True, model=None, timeout=None,
                  rng=None):
        """Append the user's line, then let each member reply in turn
        (or only the addressed member). Returns the list of
        (name, reply) utterances in speaking order."""
        self._append("user", user_text)
        for org in self.members.values():
            org.store.remember("group", f"group chat — user: {user_text}")
        target = self._addressed(user_text)
        speakers = [target] if target else self.names()
        utterances = []
        for name in speakers:
            org = self.members[name]
            reply = narration.respond(org, self.context(), model=model,
                                      timeout=timeout, rng=rng, quick=quick)
            self._append(name, reply)
            org.store.remember("group", f"group chat — I said: {reply}")
            utterances.append((name, reply))
        return utterances
