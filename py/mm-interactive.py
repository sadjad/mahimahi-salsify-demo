#!/usr/bin/env python3

import argparse
import time
import mmap
import struct
import curses
import os
import rtmidi
from collections import namedtuple


SIZEOF_UINT64_T = 8
PACKET_SIZE = 1504
OUTAGE_LENGTH_IN_MS = 1000

# 1000 packets per second
DEFAULT_MAX_BW_MBPS = float(1000 * PACKET_SIZE * 8) / (10 ** 6)

# 1 packet per second
DEFAULT_MIN_BW_MBPS = float(PACKET_SIZE * 8) / (10 ** 6)

DEFAULT_MIDI_CTRL_BW_SLIDER = 81
DEFAULT_MIDI_CTRL_DROP_BUTTON = 73
MIDI_CTRL_SLIDER_MAX = 127


# Capture all data required to output to the terminal and to the mmapped file
AppConfig = namedtuple('AppConfig', ['window', 'midi_port', 'mm', 'f',
                                     'control_file', 'max_mbps', 'min_mbps'])


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--filename', type=str,
                        default='/tmp/mm-interactive',
                        help='Path to mmap control file to')
    parser.add_argument('-m', '--midi-port', type=int,
                        help='Midi port of the device to use')
    parser.add_argument('--midi-ctrl-bw', type=int,
                        default=DEFAULT_MIDI_CTRL_BW_SLIDER,
                        help='Midi controller number for bandwidth')
    parser.add_argument('--midi-ctrl-drop', type=int,
                        default=DEFAULT_MIDI_CTRL_DROP_BUTTON,
                        help='Midi controller number for drops')
    parser.add_argument('--min', type=float, default=DEFAULT_MIN_BW_MBPS,
                        help='Min bandwidth (Mbps)')
    parser.add_argument('--max', type=float, default=DEFAULT_MAX_BW_MBPS,
                        help='Max bandwidth (Mbps)')
    parser.add_argument('--no-ui', action='store_true',
                        help='Disable curses UI')
    return parser.parse_args()


def mbps_to_pps(mbps):
    return mbps * (10 ** 6) / (8 * PACKET_SIZE)


def print_midi_message(midi):
    if midi.isNoteOn():
        print('on: ', midi.getMidiNoteName(midi.getNoteNumber()),
              midi.getVelocity())
    elif midi.isNoteOff():
        print('off:', midi.getMidiNoteName(midi.getNoteNumber()))
    elif midi.isController():
        print('controller', midi.getControllerNumber(),
              midi.getControllerValue())


def refresh_window(conf, mbps, link_on):
    pps = mbps_to_pps(mbps)
    conf.window.clear()

    line_count = 0

    def addstr(line):
        nonlocal line_count
        conf.window.addstr(line_count, 0, line)
        line_count += 1

    if conf.midi_port is None:
        addstr('Control MahiMahi with the UP/DOWN/ENTER keys')
    else:
        addstr('Control MahiMahi with midi port {}'.format(conf.midi_port))
    addstr('Control file: {}'.format(conf.control_file))
    addstr('Max bandwidth: {:.3f} Mbps'.format(conf.max_mbps))
    addstr('Min bandwidth: {:.3f} Mbps'.format(conf.min_mbps))
    addstr('Current bandwidth: {:.3f} Mbps'.format(mbps))
    addstr('Packets per second: {:.2f}'.format(pps))
    addstr('Link status: {}'.format('running' if link_on else 'dead'))
    conf.window.refresh()


def write_to_mm_region(conf, mbps, link_on):
    # The first uint64_t is the bps and the second is
    # whether the link is running
    bps = int(mbps * (10 ** 6))
    conf.mm.seek(0)
    conf.mm.write(struct.pack('=QQ', bps, 1 if link_on else 0))
    os.fsync(conf.f.fileno())


def cause_temporary_outage(conf, mbps):
    write_to_mm_region(conf, mbps, False)
    if conf.window:
        refresh_window(conf, mbps, False)
        curses.beep()
    time.sleep(OUTAGE_LENGTH_IN_MS / 1000.0)


def keyboard_loop(conf):
    assert conf.window, 'Using keyboard input requires curses UI'
    curr_bw = conf.max_mbps
    write_to_mm_region(conf, curr_bw, True)
    refresh_window(conf, curr_bw, True)
    while True:
        k = conf.window.getch()
        if k == ord('\n') or k == curses.KEY_ENTER:
            cause_temporary_outage(conf, curr_bw)
        elif k == curses.KEY_UP:
            curr_bw = min(curr_bw + 0.1, conf.max_mbps)
        elif k == curses.KEY_DOWN:
            curr_bw = max(conf.min_mbps, curr_bw - 1)
        else:
            # Ignored input
            continue

        # Update the display
        write_to_mm_region(conf, curr_bw, True)
        refresh_window(conf, curr_bw, True)


def midi_loop(conf, midi_ctrl_bw, midi_ctrl_drop):
    midiin = rtmidi.RtMidiIn()
    midiin.openPort(conf.midi_port)

    midiout = rtmidi.RtMidiOut()
    midiout.openPort(conf.midi_port)

    slider_increment = (float(conf.max_mbps - conf.min_mbps) /
                        MIDI_CTRL_SLIDER_MAX)

    curr_bw = conf.max_mbps
    write_to_mm_region(conf, curr_bw, True)
    if conf.window:
        refresh_window(conf, curr_bw, True)

    # Turn the drop light off
    midiout.sendMessage(rtmidi.MidiMessage.controllerEvent(
                        1, midi_ctrl_drop, 0))
    # Set initial slider position to maximum
    midiout.sendMessage(rtmidi.MidiMessage.controllerEvent(
                        1, midi_ctrl_bw, 127))

    while True:
        m = midiin.getMessage(250)
        if not m:
            continue

        # print_midi_message(m)
        if not m.isController():
            continue

        ctrl_no = m.getControllerNumber()
        if ctrl_no == midi_ctrl_bw:
            slider_val = m.getControllerValue()
            assert(slider_val >= 0)
            new_bw = slider_val * slider_increment + conf.min_mbps
            new_bw = min(conf.max_mbps, new_bw)
            new_bw = max(conf.min_mbps, new_bw)
            curr_bw = new_bw

        elif ctrl_no == midi_ctrl_drop:
            slider_val = int((curr_bw - conf.min_mbps) / slider_increment)
            slider_val = min(slider_val, 127)
            slider_val = max(slider_val, 0)

            # Set the slider to 0
            midiout.sendMessage(rtmidi.MidiMessage.controllerEvent(
                                1, midi_ctrl_bw, 0))

            cause_temporary_outage(conf, curr_bw)

            # Turn off the drop light
            midiout.sendMessage(rtmidi.MidiMessage.controllerEvent(
                                1, midi_ctrl_drop, 0))
            # Set the slider back
            midiout.sendMessage(rtmidi.MidiMessage.controllerEvent(
                                1, midi_ctrl_bw, slider_val))

        write_to_mm_region(conf, curr_bw, True)
        if conf.window:
            refresh_window(conf, curr_bw, True)


def init_window():
    window = curses.initscr()
    window.keypad(True)
    window.clear()
    curses.noecho()
    curses.cbreak()
    return window


def main(args):
    no_ui = args.no_ui
    control_file = args.filename
    midi_port = args.midi_port
    midi_ctrl_bw = args.midi_ctrl_bw
    midi_ctrl_drop = args.midi_ctrl_drop

    min_mbps = args.min
    max_mbps = args.max

    with open(control_file, 'wb+') as f:
        mmap_len = 2 * SIZEOF_UINT64_T
        f.write(bytes([0] * mmap_len))
        f.flush()
        mm = mmap.mmap(f.fileno(), mmap_len, prot=mmap.PROT_WRITE)

        if not no_ui:
            window = init_window()
        else:
            window = None

        conf = AppConfig(window=window, midi_port=midi_port, mm=mm, f=f,
                         control_file=control_file,
                         max_mbps=max_mbps, min_mbps=min_mbps)

        if midi_port is not None:
            midi_loop(conf, midi_ctrl_bw, midi_ctrl_drop)
        else:
            keyboard_loop(conf)


if __name__ == '__main__':
    main(get_args())
