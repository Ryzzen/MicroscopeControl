"""Letting the device panel shrink instead of scroll sideways.

Qt sizes most controls to fit their own text: a QComboBox reports a minimum
width wide enough for its current entry, a QPushButton for its label. Those
minima propagate up through the layouts and become a hard floor on the whole
panel, and a QScrollArea's only answer to a floor it cannot meet is to scroll.

A horizontal scrollbar in a settings column is the wrong outcome -- it hides
controls behind a gesture and wastes vertical space on a bar. What is wanted is
for the controls themselves to give way, since a combo showing "Large Fine JP..."
is still perfectly usable and its full text is in the popup anyway.

`QSizePolicy.Policy.Ignored` is the switch: with it set, a widget's size hint
*and* its minimum size hint are disregarded by the layout, leaving only an
explicit minimum. So each control gets a small deliberate floor -- enough to
stay tappable -- and shrinks freely below its natural width above that.
"""

from __future__ import annotations

from PySide6.QtWidgets import QSizePolicy, QWidget

# Small enough to let the panel become genuinely narrow, wide enough that a
# control never collapses to an untappable sliver on a touchscreen.
DEFAULT_MINIMUM_PX = 56


def allow_shrink(widget: QWidget, minimum: int = DEFAULT_MINIMUM_PX) -> QWidget:
    """Let `widget` be laid out narrower than its natural width.

    For controls whose content cannot reflow -- combo boxes, buttons, line
    edits. Do *not* use it on a word-wrapped QLabel: such a label already
    reports a minimum of only its longest word, and Ignored policy makes it
    take that minimum instead of the width actually available, so it wraps far
    more than it needs to.
    """
    policy = widget.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
    widget.setSizePolicy(policy)
    widget.setMinimumWidth(minimum)
    return widget


def allow_shrink_all(*widgets: QWidget, minimum: int = DEFAULT_MINIMUM_PX) -> None:
    for widget in widgets:
        allow_shrink(widget, minimum)
