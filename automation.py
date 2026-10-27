from services.workflow import run_content_workflow


class Automation:
    """Geriye uyumlu giriş noktası: iş akışını koordine eder."""

    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log

    def say(self, msg):
        self.log(msg)

    def run(self, refs=None):
        return run_content_workflow(self.cfg, refs or [], self.log)
