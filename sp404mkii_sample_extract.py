import aifc
import binascii
import os
import os.path
import pathlib
import struct
import subprocess
import sys
import wave
from argparse import ArgumentParser
from collections import namedtuple

from midiutil.MidiFile import MIDIFile

TOTAL_BANKS = 10
PADS_PER_BANK = 16
TICKS_PER_QUARTER_NOTE = 480
PADINFO_PATH = 'PADCONF.BIN'
PATTERN_DIRECTORY = 'PTN/'
SAMPLE_DIRECTORY = 'SMPL/'
BYTES_PER_NOTE = 8
MAX_PATTERNS = TOTAL_BANKS * PADS_PER_BANK

Pad = namedtuple(
    'Pad',
    'start end user_start user_end volume lofi loop gate reverse unknown1 channels tempo_mode tempo user_tempo',
)
NoteSimple = namedtuple( 'NoteSimple', 'delay pad bank_switch unknown2 velocity unknown3 length')
Note = namedtuple('Note', [*NoteSimple._fields, 'filename'])

def padtuple_to_trim_samplenums(pad: Pad) -> tuple[int, int]:
    return (pad.user_start - 512) // 2, (pad.user_end - 512) // 2


def trim_wav_by_frame_numbers(
    infile_path: pathlib.Path, outfile_path: pathlib.Path, start_frame: int, end_frame: int
):
    # https://gearspace.com/board/showpost.php?p=16482943&postcount=1696&s=7011f4122d06b258eb43da468535df12
    infile_wave_path = infile_path.with_suffix('.wav').as_posix()
    subprocess.run(
        [
            'sox',
            '-t',
            'raw',
            '-e',
            'signed',
            '-b16',
            '-c2',
            '-r',
            '48000',
            '-B',
            infile_path,
            infile_wave_path,
        ]
    )
    with (
        wave.open(infile_wave_path, "rb") as in_file,
        wave.open(outfile_path.as_posix(), "wb") as out_file,
    ):
        if end_frame == start_frame or start_frame < 0 or end_frame < 0:
            start_frame = 0
            end_frame = in_file.getnframes()
        out_length_frames = end_frame - start_frame
        out_length_frames = min(out_length_frames, in_file.getnframes())
        out_file.setparams(
            (
                in_file.getnchannels(),
                in_file.getsampwidth(),
                in_file.getframerate(),
                out_length_frames,
                in_file.getcomptype(),
                in_file.getcompname(),
            )
        )
        in_file.setpos(start_frame)
        out_file.writeframes(in_file.readframes(out_length_frames))


def create_midi_file(
    path: str,
    selected_pad: str,
    pad_layout: dict[int, Pad],
    notes: list[NoteSimple],
    midi_tempo: float,
):
    midi_file = MIDIFile(numTracks=1)
    midi_file.addTrackName(
        track=0, time=0, trackName="Roland SP-404mkii Pattern " + selected_pad.upper()
    )
    midi_file.addTempo(track=0, time=0, tempo=midi_tempo)

    note_path_to_pitch = {}
    next_available_pitch = 36  # for C1. see "midi note numbers" in http://www.sengpielaudio.com/calculator-notenames.htm
    time_in_beats_for_next_note = 0
    for note in notes:
        if note.pad != MAX_PATTERNS:
            note_filename = notetuple_to_note_filename(note) + '.SMP'
            note_path = pathlib.Path(path) / SAMPLE_DIRECTORY / note_filename
            if note_path not in note_path_to_pitch:
                note_path_to_pitch[note_path] = next_available_pitch
                next_available_pitch += 1
            if note_path.exists():
                current_pad = pad_layout[notetuple_to_sample_number(note)]
                user_start_sample, user_end_sample = padtuple_to_trim_samplenums(
                    current_pad
                )

                outfile_path = (
                    pathlib.Path(f'pad-{selected_pad}') / 'samples' / os.path.basename(note_path)
                ).with_suffix(
                    '.wav'
                )  # TODO: robust temporary filename selection

                trim_wav_by_frame_numbers(
                    note_path, outfile_path, user_start_sample, user_end_sample
                )

                length = note.length / TICKS_PER_QUARTER_NOTE
                midi_file.addNote(
                    track=0,
                    channel=0,
                    pitch=note_path_to_pitch[note_path],
                    time=time_in_beats_for_next_note,
                    duration=length,
                    volume=100,
                )
            else:
                print("skipping missing sample", file=sys.stderr)
        else:
            print("skipping empty note", file=sys.stderr)
        delay = note.delay / TICKS_PER_QUARTER_NOTE
        time_in_beats_for_next_note += delay

    create_sfz(selected_pad, note_path_to_pitch)

    with open("PTN_" + selected_pad.upper() + ".mid", 'wb') as binfile:
        midi_file.writeFile(binfile)
    # play it with "timidity output.mid" /etc/timidity/freepats.cfg
    # see eg /usr/share/midi/freepats/Tone_000/004_Electric_Piano_1_Rhodes.pat


def get_pad_info(path: str) -> dict[int, Pad]:
    # http://sp-forums.com/viewtopic.php?p=60548&sid=840a92a45a7790dd9b593f061ffb4478#p60548
    # http://sp-forums.com/viewtopic.php?p=60553#p60553
    # TODO: sanity check filesize==3840bytes==120pads*32bytes
    # TODO: don't assume user gave sd root path with trailing frontslash
    with open(pathlib.Path(path) / PADINFO_PATH, 'rb') as f:
        pads = {}
        i = 0
        while i < TOTAL_BANKS * PADS_PER_BANK:
            pad_data = f.read(32)
            pad = Pad._make(struct.unpack('>IIIIB????BBBII', pad_data))
            pads[i + 1] = pad
            i += 1
    return pads


def notetuple_to_note_filename(note: NoteSimple) -> str:
    return pad_number_to_filename(notetuple_to_sample_number(note))


def pad_number_to_filename(pad_number: int) -> str:
    pad_number -= 1
    bank_number = pad_number // PADS_PER_BANK
    bank_pad_number = (pad_number % PADS_PER_BANK) + 1
    return f'BANK{bank_number}-{bank_pad_number:02d}'


def notetuple_to_sample_number(note: NoteSimple) -> int:
    if note.bank_switch == 64 or note.bank_switch == 0:
        sample_number = note.pad - 46
    elif note.bank_switch == 65 or note.bank_switch == 1:
        sample_number = note.pad - 46 + PADS_PER_BANK * 5
    else:
        print("unexpected value for bank_switch", file=sys.stderr)
        sys.exit(1)

    return sample_number


def create_sfz(selected_pad, note_path_to_pitch):
    sfz_root = pathlib.Path(f'pad-{selected_pad}')
    sample_dir = sfz_root / 'samples'
    sample_dir.mkdir(parents=True, exist_ok=True)
    with open((sfz_root / sfz_root.with_suffix('.sfz').name).as_posix(), 'w') as sfz_file:
        sfz_file.write("""<control>
default_path={path}
<global>
<group>
""".format(path='samples'))
        # start_key = 38
        for smp_file, i in note_path_to_pitch.items():
            wave_file = pathlib.Path(smp_file).with_suffix('.wav').name
            if (sample_dir / wave_file).exists():
                sfz_file.write(f'<region> sample={wave_file} key={i}\n')


def pattern_name_to_filename(pattern_name: str) -> str:
    # TODO: User more robust parsing of pad name
    x = (ord(pattern_name[0].upper()) - ord('A')) * PADS_PER_BANK
    y = int(pattern_name[1:]) % PADS_PER_BANK
    return f'PTN{x + y:05d}.BIN'


def get_pattern(path: str, pad: str) -> list[NoteSimple]:
    # http://sp-forums.com/viewtopic.php?p=60635&sid=820f29eed0f7275dbeaf776173911736#p60635
    # http://sp-forums.com/viewtopic.php?p=60693&sid=820f29eed0f7275dbeaf776173911736#p60693
    with open(
        path + PATTERN_DIRECTORY + pattern_name_to_filename(pad), 'rb'
    ) as f:  # TODO: handle command line args w/ argparse
        ptn_filesize = os.fstat(f.fileno()).st_size
        # TODO: sanity check filesize==multiple of BYTES_PER_NOTE
        notes = []
        i = 0
        while (
            i < (ptn_filesize // BYTES_PER_NOTE) - 2
        ):  # 2*8 trailer bytes at the end of the file
            note_data = f.read(8)
            note_simple = NoteSimple._make(struct.unpack('>BBBBBBH', note_data))
            note = Note._make(
                [
                    note_simple.delay,
                    note_simple.pad,
                    note_simple.bank_switch,
                    note_simple.unknown2,
                    note_simple.velocity,
                    note_simple.unknown3,
                    note_simple.length,
                    path + PATTERN_DIRECTORY + pattern_name_to_filename(pad),
                ]
            )
            notes.append(note)

            i += 1

        ptn_trailer = f.read(16)
    return notes


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('path', action='store')
    parser.add_argument("pad", action='store')
    parser.add_argument("midi-tempo", action='store')
    args = parser.parse_args()

    path = getattr(args, 'path')
    selected_pad = getattr(args, 'pad')
    midi_tempo = getattr(args, 'midi-tempo')

    pad_layout = get_pad_info(path)
    notes = get_pattern(path, selected_pad)

    create_midi_file(path, selected_pad, pad_layout, notes, float(midi_tempo))
    # create_sfz(selected_pad, notes)
