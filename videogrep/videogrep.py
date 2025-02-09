from __future__ import print_function

import os
import re
import random
import gc
import subprocess
from glob import glob
from collections import OrderedDict

from moviepy.editor import VideoFileClip, concatenate
import audiogrep

from .vtt import parse_auto_sub
from .timecode import Timecode
from . import searcher

usable_extensions = ['mp4', 'avi', 'mov', 'mkv', 'm4v']
BATCH_SIZE = 20


def get_fps(filename):
    output = subprocess.run(['ffmpeg', '-i', filename], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,text=True).stdout
    match = re.search(r'([\d.]+) fps', output, flags=re.MULTILINE)
    try:
        return float(match.group(1))
    except:
        print("[!] Could not detect FPS; defaulting to 25.")
        return 25

def get_duration(filename):
    """Determine the video length in seconds using ffprobe"""
    output = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', filename], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout
    print(f"Output: {output}")
    return float(output)


def get_ngrams(inputfile, n=1, use_transcript=False, use_vtt=False):
    '''
    Get ngrams from a text
    Sourced from:
    https://gist.github.com/dannguyen/93c2c43f4e65328b85af
    '''

    words = []
    if use_transcript:
        for s in audiogrep.convert_timestamps(inputfile):
            for w in s['words']:
                words.append(w[0])
    elif use_vtt:
        vtts = get_vtt_files(inputfile)
        for vtt in vtts:
            with open(vtt['vtt'], 'r') as infile:
                sentences = parse_auto_sub(infile.read())
            for s in sentences:
                for w in s['words']:
                    words.append(w['word'])
    else:
        text = ''
        srts = get_subtitle_files(inputfile)
        for srt in srts:
            lines = clean_srt(srt)
            if lines:
                for timespan in lines.keys():
                    line = lines[timespan].strip()
                    text += line + ' '

        words = re.split(r'[.?!,:\"]+\s*|\s+', text)

    ngrams = zip(*[words[i:] for i in range(n)])
    return ngrams


def make_edl_segment(n, time_in, time_out, rec_in, rec_out, full_name, filename, fps=25):
    reel = full_name
    if len(full_name) > 7:
        reel = full_name[0:7]

    template = '{} {} AA/V  C        {} {} {} {}\n* FROM CLIP NAME:  {}\n* COMMENT: \n FINAL CUT PRO REEL: {} REPLACED BY: {}\n\n'

    out = template.format(
        n,
        full_name,
        Timecode(fps, start_seconds=time_in),
        Timecode(fps, start_seconds=time_out),
        Timecode(fps, start_seconds=rec_in),
        Timecode(fps, start_seconds=rec_out),
        filename,
        full_name,
        reel
    )

    return out


def make_edl(timestamps, name):
    '''Converts an array of ordered timestamps into an EDL string'''

    fpses = {}

    out = "TITLE: {}\nFCM: NON-DROP FRAME\n\n".format(name)

    rec_in = 0

    for index, timestamp in enumerate(timestamps):
        if timestamp['file'] not in fpses:
            fpses[timestamp['file']] = get_fps(timestamp['file'])

        fps = fpses[timestamp['file']]

        n = str(index + 1).zfill(4)

        time_in = timestamp['start']
        time_out = timestamp['end']
        duration = time_out - time_in

        rec_out = rec_in + duration

        full_name = 'reel_{}'.format(n)

        filename = timestamp['file']

        out += make_edl_segment(n, time_in, time_out, rec_in, rec_out, full_name, filename, fps=fps)

        rec_in = rec_out

    with open(name, 'w') as outfile:
        outfile.write(out)

def make_otio(timestamps, name):
    '''Converts an array of ordered timestamps into an OTIO'''
    import opentimelineio as otio

    # build the structure
    tl = otio.schema.Timeline(name="My timeline")
    tr = otio.schema.Track(name="Supercut")
    tl.tracks.append(tr)


    filedata = {}

    rec_in = 0

    for index, timestamp in enumerate(timestamps):
        if timestamp['file'] not in filedata:
            filedata[timestamp['file']] = get_fps(timestamp['file']), get_duration(timestamp['file'])

        fps, file_duration = filedata[timestamp['file']]

        n = str(index + 1).zfill(4)

        time_in = timestamp['start']
        time_out = timestamp['end']
        duration = time_out - time_in

        rec_out = rec_in + duration

        full_name = 'reel_{}'.format(n)

        filename = timestamp['file']

        # put the clip into the track
        tr.append(
            otio.schema.Clip(
                name=full_name,
                media_reference=otio.schema.ExternalReference(
                    target_url=filename,
                    # available range is the content available for editing
                    available_range=otio.opentime.TimeRange(
                        start_time=otio.opentime.RationalTime(0, fps),
                        duration=otio.opentime.RationalTime(file_duration * fps, fps) #TODO!!!
                    )
                ),
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(time_in * fps, fps),
                    duration=otio.opentime.RationalTime(duration * fps, fps)
                )
            )
        )

    # write the file to disk
    otio.adapters.write_to_file(tl, name)

def create_timestamps(inputfiles):
    files = audiogrep.convert_to_wav(inputfiles)
    audiogrep.transcribe(files)


def convert_timespan(timespan):
    """Convert an srt timespan into a start and end timestamp."""
    start, end = timespan.split('-->')
    start = convert_timestamp(start)
    end = convert_timestamp(end)
    return start, end


def convert_timestamp(timestamp):
    """Convert an srt timestamp into seconds."""
    timestamp = timestamp.strip()
    chunk, millis = timestamp.split(',')
    hours, minutes, seconds = chunk.split(':')
    hours = int(hours)
    minutes = int(minutes)
    seconds = int(seconds)
    seconds = seconds + hours * 60 * 60 + minutes * 60 + float(millis) / 1000
    return seconds


def clean_srt(srt):
    """Remove damaging line breaks and numbers from srt files and return a
    dictionary.
    """
    with open(srt, 'r') as f:
        text = f.read()
    text = re.sub(r'^\d+[\n\r]', '', text, flags=re.MULTILINE)
    lines = text.splitlines()
    output = OrderedDict()
    key = ''

    for line in lines:
        line = line.strip()
        if line.find('-->') > -1:
            key = line
            output[key] = ''
        else:
            if key != '':
                output[key] += line + ' '

    return output


def cleanup_log_files(outputfile):
    """Search for and remove temp log files found in the output directory."""
    d = os.path.dirname(os.path.abspath(outputfile))
    logfiles = [f for f in os.listdir(d) if f.endswith('ogg.log')]
    for f in logfiles:
        os.remove(f)


def demo_supercut(composition, padding):
    """Print out timespans to be cut followed by the line number in the srt."""
    for i, c in enumerate(composition):
        line = c['line']
        start = c['start']
        end = c['end']
        if i > 0 and composition[i - 1]['file'] == c['file'] and start < composition[i - 1]['end']:
            start = start + padding
        print("{1:.2f} to {2:.2f}:\t{0}".format(line, start, end))


def create_supercut(composition, outputfile, padding):
    """Concatenate video clips together and output finished video file to the
    output directory.
    """
    print("[+] Creating clips.")
    demo_supercut(composition, padding)

    # add padding when necessary
    for (clip, nextclip) in zip(composition, composition[1:]):
        if ((nextclip['file'] == clip['file']) and (nextclip['start'] < clip['end'])):
            nextclip['start'] += padding

    # put all clips together:
    all_filenames = set([c['file'] for c in composition])
    videofileclips = dict([(f, VideoFileClip(f)) for f in all_filenames])
    cut_clips = [videofileclips[c['file']].subclip(c['start'], c['end']) for c in composition]

    print("[+] Concatenating clips.")
    final_clip = concatenate(cut_clips)

    print("[+] Writing ouput file.")
    final_clip.to_videofile(outputfile, codec="libx264", temp_audiofile='temp-audio.m4a', remove_temp=True, audio_codec='aac')



def create_supercut_in_batches(composition, outputfile, padding):
    """Create & concatenate video clips in groups of size BATCH_SIZE and output
    finished video file to output directory.
    """
    total_clips = len(composition)
    start_index = 0
    end_index = BATCH_SIZE
    batch_comp = []
    while start_index < total_clips:
        filename = outputfile + '.tmp' + str(start_index) + '.mp4'
        try:
            create_supercut(composition[start_index:end_index], filename, padding)
            batch_comp.append(filename)
            gc.collect()
            start_index += BATCH_SIZE
            end_index += BATCH_SIZE
        except:
            start_index += BATCH_SIZE
            end_index += BATCH_SIZE
            next

    clips = [VideoFileClip(filename) for filename in batch_comp]
    video = concatenate(clips)
    video.to_videofile(outputfile, codec="libx264", temp_audiofile='temp-audio.m4a', remove_temp=True, audio_codec='aac')


    # remove partial video files
    for filename in batch_comp:
        os.remove(filename)

    cleanup_log_files(outputfile)


def split_clips(composition, outputfile):
    all_filenames = set([c['file'] for c in composition])
    videofileclips = dict([(f, VideoFileClip(f)) for f in all_filenames])
    cut_clips = [videofileclips[c['file']].subclip(c['start'], c['end']) for c in composition]

    basename, ext = os.path.splitext(outputfile)
    print("[+] Writing ouput files.")
    for i, clip in enumerate(cut_clips):
        clipfilename = basename + '_' + str(i).zfill(5) + ext
        clip.to_videofile(clipfilename, codec="libx264", temp_audiofile='temp-audio.m4a', remove_temp=True, audio_codec='aac')


def search_line(line, search, searchtype):
    """Return True if search term is found in given line, False otherwise."""
    if searchtype == 're' or searchtype == 'word':
        return re.search(search, line)  #, re.IGNORECASE)
    elif searchtype == 'pos':
        return searcher.search_out(line, search)
    elif searchtype == 'hyper':
        return searcher.hypernym_search(line, search)


def get_subtitle_files(inputfile):
    """Return a list of subtitle files."""
    srts = []

    for f in inputfile:
        filename = f.split('.')
        filename[-1] = 'srt'
        srt = '.'.join(filename)
        if os.path.isfile(srt):
            srts.append(srt)

    if len(srts) == 0:
        print("[!] No subtitle files were found.")
        return False

    return srts


def get_vtt_files(inputfile):
    """Return a list of vtt files."""
    vtts = []

    for f in inputfile:
        filename = f.split('.')
        filename = '.'.join(filename[0:-1])
        vtt = glob(filename + '*.vtt')
        if len(vtt) > 0:
            vtts.append({'vtt': vtt[0], 'video': f})

    if len(vtts) == 0:
        print("[!] No vtt files were found.")
        return False

    return vtts


def compose_from_srts(srts, search, searchtype):
    """Takes a list of subtitle (srt) filenames, search term and search type
    and, returns a list of timestamps for composing a supercut.
    """
    composition = []
    foundSearchTerm = False

    # Iterate over each subtitles file.
    for srt in srts:

        print(srt)
        lines = clean_srt(srt)

        videofile = ""
        foundVideoFile = False

        print("[+] Searching for video file corresponding to '" + srt + "'.")
        for ext in usable_extensions:
            tempVideoFile = srt.replace('.srt', '.' + ext)
            if os.path.isfile(tempVideoFile):
                videofile = tempVideoFile
                foundVideoFile = True
                print("[+] Found '" + tempVideoFile + "'.")

        # If a correspndong video file was found for this subtitles file...
        if foundVideoFile:

            # Check that the subtitles file contains subtitles.
            if lines:

                # Iterate over each line in the current subtitles file.
                for timespan in lines.keys():
                    line = lines[timespan].strip()

                    # If this line contains the search term
                    if search_line(line, search, searchtype):

                        foundSearchTerm = True

                        # Extract the timespan for this subtitle.
                        start, end = convert_timespan(timespan)

                        # Record this occurance of the search term.
                        composition.append({'file': videofile, 'time': timespan, 'start': start, 'end': end, 'line': line})

                # If the search was unsuccessful.
                if foundSearchTerm is False:
                    print("[!] Search term '" + search + "'" + " was not found is subtitle file '" + srt + "'.")

            # If no subtitles were found in the current file.
            else:
                print("[!] Subtitle file '" + srt + "' is empty.")

        # If no video file was found...
        else:
            print("[!] No video file was found which corresponds to subtitle file '" + srt + "'.")
            print("[!] The following video formats are currently supported:")
            extList = ""
            for ext in usable_extensions:
                extList += ext + ", "
            print(extList)

    return composition


def compose_from_transcript(files, search, searchtype):
    """Takes transcripts created by audiogrep/pocketsphinx, a search and search type
    and returns a list of timestamps for creating a supercut"""

    final_segments = []

    if searchtype in ['re', 'word', 'franken', 'fragment']:
        if searchtype == 're':
            searchtype = 'sentence'

        segments = audiogrep.search(search, files, mode=searchtype, regex=True)
        for seg in segments:
            seg['file'] = seg['file'].replace('.transcription.txt', '')
            seg['line'] = seg['words']
            final_segments.append(seg)

    elif searchtype in ['hyper', 'pos']:
        for s in audiogrep.convert_timestamps(files):
            for w in s['words']:
                if search_line(w[0], search, searchtype):
                    seg = {
                        'file': s['file'].replace('.transcription.txt',''),
                        'line': w[0],
                        'start': float(w[1]),
                        'end': float(w[2])
                    }
                    final_segments.append(seg)

    return final_segments


def compose_from_vtt(files, search, searchtype):
    final_segments = []

    for f in files:
        video = f['video']

        with open(f['vtt'], 'r') as infile:
            sentences = parse_auto_sub(infile.read())

        for sentence in sentences:
            if searchtype in ['word', 'hyper', 'pos']:
                for word in sentence['words']:
                    if search_line(word['word'], search, searchtype):
                        seg = {
                            'file': video,
                            'line': word['word'],
                            'start': word['start'],
                            'end': word['end']
                        }
                        final_segments.append(seg)
            else:
                if search_line(sentence['text'], search, searchtype):
                    seg = {
                        'file': video,
                        'line': sentence['text'],
                        'start': sentence['start'],
                        'end': sentence['end']
                    }
                    final_segments.append(seg)

    return final_segments


def videogrep(inputfile, outputfile, search, searchtype, maxclips=0, padding=0, test=False, randomize=False, sync=0, use_transcript=False, use_vtt=False, export_clips=False):
    """Search through and find all instances of the search term in an srt or transcript,
    create a supercut around that instance, and output a new video file
    comprised of those supercuts.
    """

    padding = padding / 1000.0
    sync = sync / 1000.0
    composition = []
    foundSearchTerm = False

    if use_transcript:
        composition = compose_from_transcript(inputfile, search, searchtype)
    elif use_vtt:
        vtts = get_vtt_files(inputfile)
        composition = compose_from_vtt(vtts, search, searchtype)
    else:
        srts = get_subtitle_files(inputfile)
        composition = compose_from_srts(srts, search, searchtype)


    # If the search term was not found in any subtitle file...
    if len(composition) == 0:
        print("[!] Search term '" + search + "'" + " was not found in any file.")
        exit(1)

    else:
        print("[+] Search term '" + search + "'" + " was found in " + str(len(composition)) + " places.")

        # apply padding and sync
        for c in composition:
            c['start'] = c['start'] + sync - padding
            c['end'] = c['end'] + sync + padding

        if maxclips > 0:
            composition = composition[:maxclips]

        if randomize is True:
            random.shuffle(composition)

        if test is True:
            demo_supercut(composition, padding)
        else:
            if os.path.splitext(outputfile)[1].lower() == '.edl':
                make_edl(composition, outputfile)
            if os.path.splitext(outputfile)[1].lower() == '.otio':
                make_otio(composition, outputfile)
            elif export_clips:
                split_clips(composition, outputfile)
            else:
                if len(composition) > BATCH_SIZE:
                    print("[+] Starting batch job.")
                    create_supercut_in_batches(composition, outputfile, padding)
                else:
                    create_supercut(composition, outputfile, padding)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Generate a "supercut" of one or more video files by searching through subtitle tracks.')
    parser.add_argument('--input', '-i', dest='inputfile', nargs='*', required=True, help='video or subtitle file, or folder')
    parser.add_argument('--search', '-s', dest='search', help='search term')
    parser.add_argument('--search-type', '-st', dest='searchtype', default='re', choices=['re', 'pos', 'hyper', 'fragment', 'franken', 'word'], help='type of search')
    parser.add_argument('--use-transcript', '-t', action='store_true', dest='use_transcript', help='Use a transcript generated by pocketsphinx instead of srt files')
    parser.add_argument('--use-vtt', '-vtt', action='store_true', dest='use_vtt', help='Use a vtt file instead of srt')
    parser.add_argument('--max-clips', '-m', dest='maxclips', type=int, default=0, help='maximum number of clips to use for the supercut')
    parser.add_argument('--output', '-o', dest='outputfile', default='supercut.mp4', help='name of output file')
    parser.add_argument('--export-clips', '-ec', dest='export_clips', action='store_true', help='Export individual clips')
    parser.add_argument('--demo', '-d', action='store_true', help='show results without making the supercut')
    parser.add_argument('--randomize', '-r', action='store_true', help='randomize the clips')
    parser.add_argument('--youtube', '-yt', help='grab clips from youtube based on your search')
    parser.add_argument('--padding', '-p', dest='padding', default=0, type=int, help='padding in milliseconds to add to the start and end of each clip')
    parser.add_argument('--resyncsubs', '-rs', dest='sync', default=0, type=int, help='Subtitle re-synch delay +/- in milliseconds')
    parser.add_argument('--transcribe', '-tr', dest='transcribe', action='store_true', help='Transcribe the video using audiogrep. Requires pocketsphinx')
    parser.add_argument('--ngrams', '-n', dest='ngrams', type=int, default=0,  help='Return ngrams for videos')

    args = parser.parse_args()

    if not args.transcribe and args.ngrams == 0:
        if args.search is None:
             parser.error('argument --search/-s is required')

    if args.transcribe:
        create_timestamps(args.inputfile)
    elif args.ngrams > 0:
        from collections import Counter
        grams = get_ngrams(args.inputfile, args.ngrams, args.use_transcript, args.use_vtt)
        most_common = Counter(grams).most_common(100)
        for ngram, count in most_common:
            print(' '.join(ngram), count)
    else:
        videogrep(args.inputfile, args.outputfile, args.search, args.searchtype, args.maxclips, args.padding, args.demo, args.randomize, args.sync, args.use_transcript, args.use_vtt, args.export_clips)


if __name__ == '__main__':
    main()

