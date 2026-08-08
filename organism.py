import scallopy

BEL = "bel"
PROVENANCE = "minmaxprob"


class Mind:
    """The Scallop program. Rebuilds the context from the .scl genome, runs it,
    and exposes belief facts with their minmaxprob confidences."""

    def __init__(self, scl_path):
        self.scl_path = scl_path
        self.ctx = None

    def rebuild(self):
        self.ctx = scallopy.ScallopContext(provenance=PROVENANCE)
        if self.scl_path.exists():
            self.ctx.import_file(str(self.scl_path))
        self.ctx.run()

    def beliefs(self):
        out = {}
        for tag, tup in self.ctx.relation(BEL):
            out[tuple(tup)] = float(tag)
        return out

    def query_rule(self, rule, head_relation):
        """Run a candidate rule against a fork of the current program without
        committing. Returns list of (tag, tuple)."""
        ctx = scallopy.ScallopContext(provenance=PROVENANCE, fork_from=self.ctx)
        ctx.add_rule(rule)
        ctx.run()
        return [(float(tag), tuple(tup)) for (tag, tup) in ctx.relation(head_relation)]
