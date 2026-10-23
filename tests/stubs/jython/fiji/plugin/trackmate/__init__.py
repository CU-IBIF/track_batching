from java.util import HashMap
from java.lang import Double, Integer, Boolean
class Logger(object):
    IJ_LOGGER = object()
class Spot(object):
    def __init__(self, i, t): self.i = i; self.f = {"POSITION_X": float(i), "FRAME": float(t), "QUALITY": 5.0}
    def ID(self): return self.i
    def getFeature(self, n): return self.f.get(n)
class SpotColl(object):
    def __init__(self, spots): self.spots = spots
    def iterable(self, vis): return iter(self.spots)
class TrackModel(object):
    def __init__(self, spots): self.spots = spots
    def trackIDOf(self, s): return 0 if s.i < 3 else None
    def trackIDs(self, vis): return set([0])
    def trackEdges(self, tid): return [(self.spots[0], self.spots[1]), (self.spots[1], self.spots[2])]
    def getEdgeSource(self, e): return e[0]
    def getEdgeTarget(self, e): return e[1]
class FeatureModel(object):
    def getSpotFeatures(self): return ["POSITION_X", "FRAME", "QUALITY", "MEAN_INTENSITY_CH1"]
    def getEdgeFeatures(self): return ["LINK_COST"]
    def getEdgeFeature(self, e, n): return 1.5
    def getTrackFeatures(self): return ["NUMBER_SPOTS", "TRACK_DURATION"]
    def getTrackFeature(self, tid, n): return 3.0
class Model(object):
    def __init__(self):
        self.spots = [Spot(i, i) for i in range(4)]
    def setLogger(self, l): pass
    def getSpots(self): return SpotColl(self.spots)
    def getTrackModel(self): return TrackModel(self.spots)
    def getFeatureModel(self): return FeatureModel()
class Settings(object):
    def __init__(self, imp):
        self.imp = imp; self.spotFilters = []; self.trackFilters = []; self.initialSpotFilterValue = None
    def addAllAnalyzers(self): pass
    def addSpotFilter(self, f): self.spotFilters.append(f)
    def addTrackFilter(self, f): self.trackFilters.append(f)
class TrackMate(object):
    def __init__(self, model, settings): self.settings = settings
    def setNumThreads(self, n): pass
    def checkInput(self): return True
    def process(self):
        s = self.settings
        for k in ("RADIUS",):
            v = s.detectorSettings.get(k)
            assert v.__class__ is float, (k, type(v))
        print("PROCESS det=%s trk=%s spotfilters=%d init=%s" % (dict(s.detectorSettings), s.trackerSettings.get("LINKING_FEATURE_PENALTIES"), len(s.spotFilters), s.initialSpotFilterValue))
        return True
