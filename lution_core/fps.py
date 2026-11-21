# Vendored from Wookhq/Lution @ 303be659833a (MIT, Copyright (c) 2026 WookHQ).
# Import paths are package-relative; everything else is upstream.
# See NOTICE.md next to this package.
# this for changing fps cap simple enough

from pathlib import Path
import xml.etree.ElementTree as ET

from . import paths

SOBER_APP_ID = "org.vinegarhq.Sober"


def fps_xml():
    return (paths.lution_home() / ".var/app" / SOBER_APP_ID /
            "data/sober/appData/GlobalBasicSettings_13.xml")


def load_framerate_cap():
    xml = fps_xml()
    if not xml.exists():
        return None
    tree = ET.parse(xml)
    node = tree.getroot().find(".//int[@name='FramerateCap']")
    return node.text if node is not None else None


def save_framerate_cap(value):
    xml = fps_xml()
    tree = ET.parse(xml)
    node = tree.getroot().find(".//int[@name='FramerateCap']")
    if node is None:
        raise ValueError("FramerateCap node not found in XML")
    node.text = str(value)
    tree.write(xml, xml_declaration=True, encoding="UTF-8")


def validate_digits(new_value):
    return new_value == "" or (new_value.isdigit() and len(new_value) <= 6)


def __getattr__(name):
    if name == "FPS_XML":
        return fps_xml()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
