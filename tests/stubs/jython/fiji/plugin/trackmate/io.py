class TmXmlWriter(object):
    def __init__(self, f): self.f = f
    def appendModel(self, m): pass
    def appendSettings(self, s): pass
    def writeToFile(self): open(self.f.getPath(), "w").write("<TrackMate/>")
