# Copyright 2014 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Base class for vim-like keysequence parser."""

import re
import logging
from functools import partial

from PyQt5.QtCore import pyqtSignal, Qt, QObject, QTimer
from PyQt5.QtGui import QKeySequence

import qutebrowser.config.config as config


class KeyParser(QObject):

    """Parser for vim-like key sequences.

    Not intended to be instantiated directly. Subclasses have to override
    execute() to do whatever they want to.

    Class Attributes:
        MATCH_PARTIAL: Constant for a partial match (no keychain matched yet,
                       but it's still possible in the future.
        MATCH_DEFINITIVE: Constant for a full match (keychain matches exactly).
        MATCH_AMBIGUOUS: There are both a partial and a definitive match.
        MATCH_NONE: Constant for no match (no more matches possible).
        supports_count: If the keyparser should support counts.

    Attributes:
        _keystring: The currently entered key sequence
        _timer: QTimer for delayed execution.
        bindings: Bound keybindings
        modifier_bindings: Bound modifier bindings.

    Signals:
        keystring_updated: Emitted when the keystring is updated.
                           arg: New keystring.
    """

    keystring_updated = pyqtSignal(str)

    MATCH_PARTIAL = 0
    MATCH_DEFINITIVE = 1
    MATCH_AMBIGUOUS = 2
    MATCH_NONE = 3

    supports_count = False

    def __init__(self, parent=None, bindings=None, modifier_bindings=None):
        super().__init__(parent)
        self._timer = None
        self._keystring = ''
        self.bindings = {} if bindings is None else bindings
        self.modifier_bindings = ({} if modifier_bindings is None
                                  else modifier_bindings)

    def _handle_modifier_key(self, e):
        """Handle a new keypress with modifiers.

        Return True if the keypress has been handled, and False if not.

        Args:
            e: the KeyPressEvent from Qt.

        Return:
            True if event has been handled, False otherwise.
        """
        modmask2str = {
            Qt.ControlModifier: 'Ctrl',
            Qt.AltModifier: 'Alt',
            Qt.MetaModifier: 'Meta',
            Qt.ShiftModifier: 'Shift'
        }
        if e.key() in [Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta]:
            # Only modifier pressed
            return False
        mod = e.modifiers()
        modstr = ''
        if not mod & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            # won't be a shortcut with modifiers
            return False
        for (mask, s) in modmask2str.items():
            if mod & mask:
                modstr += s + '+'
        keystr = QKeySequence(e.key()).toString()
        try:
            cmdstr = self.modifier_bindings[modstr + keystr]
        except KeyError:
            logging.debug('No binding found for {}.'.format(modstr + keystr))
            return True
        self.execute(cmdstr)
        return True

    def _handle_single_key(self, e):
        """Handle a new keypress with a single key (no modifiers).

        Separate the keypress into count/command, then check if it matches
        any possible command, and either run the command, ignore it, or
        display an error.

        Args:
            e: the KeyPressEvent from Qt.
        """
        logging.debug('Got key: {} / text: "{}"'.format(e.key(), e.text()))
        txt = e.text().strip()
        if not txt:
            logging.debug('Ignoring, no text')
            return

        self._stop_delayed_exec()
        self._keystring += txt

        if self.supports_count:
            (countstr, cmd_input) = re.match(r'^(\d*)(.*)',
                                             self._keystring).groups()
            count = int(countstr) if countstr else None
        else:
            cmd_input = self._keystring
            count = None

        if not cmd_input:
            return

        (match, binding) = self._match_key(cmd_input)

        if match == self.MATCH_DEFINITIVE:
            self._keystring = ''
            self.execute(binding, count)
        elif match == self.MATCH_AMBIGUOUS:
            self._handle_ambiguous_match(binding, count)
        elif match == self.MATCH_PARTIAL:
            logging.debug('No match for "{}" (added {})'.format(
                self._keystring, txt))
        elif match == self.MATCH_NONE:
            logging.debug('Giving up with "{}", no matches'.format(
                self._keystring))
            self._keystring = ''

    def _match_key(self, cmd_input):
        """Try to match a given keystring with any bound keychain.

        Args:
            cmd_input: The command string to find.

        Return:
            A tuple (matchtype, binding).
                matchtype: MATCH_DEFINITIVE, MATCH_AMBIGUOUS, MATCH_PARTIAL or
                           MATCH_NONE
                binding: - None with MATCH_PARTIAL/MATCH_NONE
                         - The found binding with MATCH_DEFINITIVE/
                           MATCH_AMBIGUOUS
        """
        # A (cmd_input, binding) tuple (k, v of bindings) or None.
        definitive_match = None
        partial_match = False
        # Check definitive match
        try:
            definitive_match = (cmd_input, self.bindings[cmd_input])
        except KeyError:
            pass
        # Check partial match
        for binding in self.bindings:
            if definitive_match is not None and binding == definitive_match[0]:
                # We already matched that one
                continue
            if len(binding) < len(cmd_input):
                # binding is shorter than cmd_input, so it can't possibly match
                continue
            elif cmd_input[-1] == binding[len(cmd_input) - 1]:
                partial_match = True
                break
        if definitive_match is not None and partial_match:
            return (self.MATCH_AMBIGUOUS, definitive_match[1])
        elif definitive_match is not None:
            return (self.MATCH_DEFINITIVE, definitive_match[1])
        elif partial_match:
            return (self.MATCH_PARTIAL, None)
        else:
            return (self.MATCH_NONE, None)

    def _stop_delayed_exec(self):
        """Stop a delayed execution if any is running."""
        if self._timer is not None:
            logging.debug("Stopping delayed execution.")
            self._timer.stop()
            self._timer = None

    def _handle_ambiguous_match(self, binding, count):
        """Handle an ambiguous match.

        Args:
            binding: The command-string to execute.
            count: The count to pass.
        """
        logging.debug("Ambiguous match for \"{}\"".format(self._keystring))
        time = config.get('general', 'cmd_timeout')
        if time == 0:
            # execute immediately
            self._keystring = ''
            self.execute(binding, count)
        else:
            # execute in `time' ms
            logging.debug("Scheduling execution of {} in {}ms".format(binding,
                                                                      time))
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(time)
            self._timer.timeout.connect(partial(self.delayed_exec, binding,
                                                count))
            self._timer.start()

    def _normalize_keystr(self, keystr):
        """Normalize a keystring like Ctrl-Q to a keystring like Ctrl+Q.

        Args:
            keystr: The key combination as a string.

        Return:
            The normalized keystring.
        """
        replacements = [
            ('Control', 'Ctrl'),
            ('Windows', 'Meta'),
            ('Mod1', 'Alt'),
            ('Mod4', 'Meta'),
        ]
        for (orig, repl) in replacements:
            keystr = keystr.replace(orig, repl)
        for mod in ['Ctrl', 'Meta', 'Alt', 'Shift']:
            keystr = keystr.replace(mod + '-', mod + '+')
        keystr = QKeySequence(keystr).toString()
        return keystr

    def delayed_exec(self, command, count):
        """Execute a delayed command.

        Args:
            command/count: As if passed to self.execute()

        Emit:
            keystring_updated to do a delayed update.
        """
        logging.debug("Executing delayed command now!")
        self._timer = None
        self._keystring = ''
        self.keystring_updated.emit(self._keystring)
        self.execute(command, count)

    def execute(self, cmdstr, count=None):
        """Execute an action when a binding is triggered."""
        raise NotImplementedError

    def handle(self, e):
        """Handle a new keypress and call the respective handlers.

        Args:
            e: the KeyPressEvent from Qt

        Emit:
            keystring_updated: If a new keystring should be set.
        """
        handled = self._handle_modifier_key(e)
        if not handled:
            self._handle_single_key(e)
            self.keystring_updated.emit(self._keystring)
