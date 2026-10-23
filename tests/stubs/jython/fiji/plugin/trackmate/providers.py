from java.util import HashMap
from java.lang import Double, Integer, Boolean
class _F(object):
    def __init__(self, key, defaults): self.key = key; self.d = defaults
    def getKey(self): return self.key
    def getDefaultSettings(self):
        m = HashMap()
        for k, v in self.d.items(): m.put(k, v)
        return m
    def checkSettings(self, m): return True
    def checkSettingsValidity(self, m): return True
    def getErrorMessage(self): return "err"
class DetectorProvider(object):
    def getFactory(self, key):
        if key != "LOG_DETECTOR": return None
        return _F(key, {"RADIUS": Double(5.0), "TARGET_CHANNEL": Integer(1), "THRESHOLD": Double(0.0), "DO_MEDIAN_FILTERING": Boolean(False), "DO_SUBPIXEL_LOCALIZATION": Boolean(True)})
class TrackerProvider(object):
    def getFactory(self, key):
        return _F(key, {"LINKING_MAX_DISTANCE": Double(15.0), "LINKING_FEATURE_PENALTIES": HashMap(), "MAX_FRAME_GAP": Integer(2), "BLOCKING_VALUE": Double(1e300)})
