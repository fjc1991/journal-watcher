# dmgbuild settings: the look of the disk image (run by build_app.sh:  dmgbuild -s dmg_settings.py -D app=… -D bg=… -D icon=… "Journal Watcher" out.dmg)
import os

_app = defines["app"]                                   # noqa: F821  (dmgbuild provides `defines`)
_name = os.path.basename(_app)

format = "UDZO"
files = [_app]
symlinks = {"Applications": "/Applications"}
icon = defines["icon"]                                  # noqa: F821
background = defines["bg"]                              # noqa: F821
window_rect = ((200, 140), (660, 470))      # the outer size: the title bar and the status bar take about 120 of it, the picture is 660 x 350
icon_size = 128
text_size = 13
icon_locations = {_name: (170, 160), "Applications": (490, 160)}
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
arrange_by = None
