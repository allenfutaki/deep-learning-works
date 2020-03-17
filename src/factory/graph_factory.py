from src.graph.simple_classifier import SimpleClassifier
from src.graph.centernet_object_detection import CenterNetObjectDetection
from src.graph.shufflenetv2plus_classification import ShuffleNetV2PlusClassifier
from src.graph.hourglass_object_detection import HourglassObjectDetection
from src.graph.shufflenetv2_spos import ShuffleNetv2SPOS

class GraphFactory:
    products = {
        'simple_classifier': SimpleClassifier,
        'centernet_object_detection': CenterNetObjectDetection,
        'shufflenetv2plus_classification': ShuffleNetV2PlusClassifier,
        'hourglass_object_detection': HourglassObjectDetection,
        'shufflenetv2_spos_classification': ShuffleNetv2SPOS,
    }

    @classmethod
    def get_products(cls):
        return list(cls.products.keys())

    @classmethod
    def produce(cls, cfg, graph=None):
        if cfg.GRAPH not in cls.products:
            raise KeyError
        else:
            return cls.products[cfg.GRAPH if graph is None else graph](cfg)
