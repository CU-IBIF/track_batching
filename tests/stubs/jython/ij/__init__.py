class Cal(object):
    pixelWidth = 1.0; pixelHeight = 1.0; pixelDepth = 1.0; frameInterval = 1.0
    def setUnit(self, u): self.unit = u
    def setTimeUnit(self, u): self.tunit = u
class Imp(object):
    def __init__(self, path): self.path = path; self.dims = [64, 64, 1, 20, 1]; self.cal = Cal()
    def getDimensions(self): return self.dims
    def setDimensions(self, c, z, t): self.dims = [64, 64, c, z, t]
    def getCalibration(self): return self.cal
    def close(self): pass
class IJ(object):
    @staticmethod
    def openImage(p):
        if "broken" in p: return None
        return Imp(p)
