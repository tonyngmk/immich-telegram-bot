# Video colour: gallery preview investigation log

Status: **improved, not fully resolved.** Test clip 1 (naive transcode) looked
washed out ("whiter"). Test clip 2 (mobius tone map) looks better but colour
is still slightly off vs the iPhone original.

Source facts (all measured on `IMG_1516.mov`, 13.8 MB, 1920x1080, 30fps):
- Video codec `hvc1` (HEVC Main 10), audio `mp4a`, container `.mov`.
- Colour: `yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67)` + `DOVI configuration
  record, profile 8` → **Dolby Vision P8.4 over an HLG base layer, 10-bit**.
- Telegram previews HEVC-in-MOV poorly (black tiles / won't play inline),
  which was the original "videos look strange" complaint.

## Attempt 1 — H.264 transcode, no tone mapping (commit `6539521`)

Command equivalent:
`ffmpeg -i SRC -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p
-vf "scale=w='min(1280,iw)':h=-2" -c:a aac -b:a 128k -movflags +faststart OUT.mp4`

- Result: 13.8 MB → 4.3 MB, `avc1`, moof/mdat order correct (faststart OK),
  plays inline with thumbnails. Playback fixed.
- Side effect: HDR squeezed to SDR with no mapping → lifted/washed-out image.
  Measured frame stats (two scenes): mean brightness ~152–158 (vs ~118–121
  after mapping), 1.4–1.9% pixels fully blown (min channel > 235).
- Conclusion: correct container/codec fix, wrong colour handling.

## Attempt 2 — algorithm shootout (scratch files in /tmp, since removed)

ffmpeg 9.0.1 (homebrew) filter inventory: `tonemap` present; `zscale`,
`tonemap_videotoolbox`, `tonemap_opencl` **absent**; `tonemap=bt2390`
rejected at parse time. Candidates actually rendered (3 s slices, same
settings as Attempt 1 + tone map):

| variant              | scene A mean | blown | crushed | sat   | scene B mean | verdict  |
|----------------------|-------------|-------|---------|-------|-------------|----------|
| naive (no tonemap)   | 152.4       | 1.85% | 4.5%    | 0.110 | 158.4       | washed   |
| hable                | 49.6        | 0%    | 12.7%   | 0.111 | —           | crushed  |
| hable peak=10        | 49.6        | 0%    | 12.7%   | 0.111 | 51.2        | crushed  |
| mobius               | 118.6       | 0%    | 4.5%    | 0.110 | 121.3       | **kept** |
| mobius peak=10       | 118.6       | 0%    | 4.5%    | 0.110 | 121.3       | = mobius |
| reinhard             | 96.6        | 0%    | 4.3%    | 0.109 | 98.6        | dark-ish |

(mean = avg pixel brightness 0–255; blown = % pixels with min channel > 235;
crushed = % with max channel < 16; sat = mean HSV saturation. Naive crushed %
is the scene baseline — mobius/reinhard add no extra crush, hable doubles it.)

- `peak=10` changed nothing for HLG input on mobius/hable.
- mobius preserved saturation exactly (0.110/0.156 both scenes) while removing
  washout and blown highlights → chosen.

## Attempt 3 — HDR-gated mobius in `bot.py` (commit `e44f6bc`, current)

- `probe_video_info()` parses `ffmpeg -i` stderr: codec from `Video: …`,
  transfer from the pixel-format paren group (gotcha: the *first* paren is the
  profile, e.g. `(Main 10)` — must scan all groups for the `a/b/c` triple),
  plus `DOVI configuration record` detection.
- `convert_video_for_gallery()` applies `,tonemap=mobius:desat=0` **only**
  when HDR is detected; SDR path byte-identical to Attempt 1. Output tagged
  `-colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv`.
- Measured on the sample via the real code path: mean 121.3, 0% blown,
  saturation preserved. Test clip 2 (gallery msg `24309`) sent for eyeball check.
- User verdict: better, still slightly off. Test msgs `24307` (Attempt 1) and
  `24309` (Attempt 3) kept in the gallery thread for A/B until confirmed.

## Attempt 4 — Apple's own pipeline via AVFoundation (current)

Rationale: the phone app looks right because it tone-maps on-device with
Apple's VideoToolbox/HDR pipeline. Instead of imitating that curve with
ffmpeg software filters, use Apple directly: `tools/avconvert.swift` drives
`AVAssetExportSession` (720p → 540p fallback, `shouldOptimizeForNetworkUse`
for faststart). `bot.py` prefers it when built
(`swiftc -O tools/avconvert.swift -o .venv/bin/avconvert`, override with
`AVCONVERT_BIN`), falling back to ffmpeg+mobius, then to as-is upload.
Already-H.264 MP4s still skip re-encoding (probe shortcut).

Measured on the sample: 13.8 MB HEVC-DV → 12 MB H.264 SDR bt709 720x1280
(portrait kept), ~12 s. Frame stats: mean ~132, 0% blown, crush ≈ baseline —
but crucially saturation 0.22–0.25 vs 0.11–0.16 for *every* ffmpeg variant.
That 2× saturation gap is the strongest evidence yet for why mobius looked
"duller than the phone": software curves preserved luma distribution while
muting chroma relative to Apple's rendering.

Gotcha hit while wiring: `sys.executable.resolve()` escapes the venv
(Homebrew symlink) — helper lookup uses `sys.prefix/bin` instead.

Still open: user eyeball check (TEST 3, gallery msg `24352`).
**Update:** user confirms Attempt 4 "looks very good" — Apple pipeline
adopted as the gallery video path. TEST 1/2/3 messages left in the gallery
thread for reference; delete on confirmation. If anything still differs
from the phone share-sheet upload, remaining suspects are Telegram server
re-encode (#3 below) and reference ambiguity (#4).

## Open hypotheses for the residual difference

1. **Software curve choice (now fallback-only).** Hable crushed, reinhard
   darkened; mobius was kept but
   may still differ from Apple's rendering. Untried: `tonemap=clip/gamma/
   linear` blends, `desat` tuning, `peak` semantics for HLG (tonemap's peak
   models PQ nits; HLG nominal peak handling may need `peak` + exposure bias).
2. **Missing zscale path.** The reference HLG→SDR chain
   (`zscale` linearise → tone map → `zscale` to bt709) isn't available in this
   ffmpeg build (no libzimg). The current chain tone-maps in-display-referred
   space and relies on `format=yuv420p` + tags for the gamut move, which is
   approximate for bt2020→bt709 primaries.
3. **Telegram server re-encode.** Gallery videos are re-encoded by Telegram
   after upload; its converter may shift levels/saturation regardless of what
   we send. Decisive test: upload the *same* MP4 via the official app and
   compare with the bot-posted copy.
4. **Reference ambiguity.** "Correct" is the iPhone Photos rendering
   (Apple's own DV tone mapping + display processing). Attempt 4 uses that
   same Apple pipeline on the Mac side, so any leftover delta likely lives in
   Telegram's re-encode (#3) or the 720p/H.264 preview compromise (#5).
5. **10→8-bit + 4:2:0 + CRF 23.** Subtle banding/softness vs original is
   expected; not colour per se, but contributes to "off" feel.

## Suggested next steps (not yet attempted)

- Official-app upload of the identical file to isolate Telegram re-encode effects.
- Ask what specifically still looks off (skin tones? skies? overall warmth?) to
  pick the next knob instead of shooting blind.
