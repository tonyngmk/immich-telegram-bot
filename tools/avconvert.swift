// avconvert: HDR-aware gallery preview export using Apple's own pipeline.
//
// iPhone clips are Dolby Vision / HLG HDR. Exporting them through
// AVFoundation to an SDR H.264 preset applies Apple's native HDR→SDR tone
// mapping — the same family of processing the Photos app and the Telegram
// iOS share-sheet transcoder use — so previews match what the phone shows
// far better than ffmpeg's software tone curves.
//
// Usage: avconvert <input.mov> <output.mp4> [720|540]
//   Prints "<bytes>" on success. Exit codes: 0 ok, 1 usage/io,
//   2 no export session, 3 export failed/cancelled, 4 output too big/empty.
//
// Build: swiftc -O tools/avconvert.swift -o .venv/bin/avconvert
import AVFoundation
import Foundation

func fail(_ code: Int32, _ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(code)
}

let args = CommandLine.arguments
guard args.count >= 3 else { fail(1, "usage: avconvert <input> <output> [720|540]") }
let srcURL = URL(fileURLWithPath: args[1])
let dstURL = URL(fileURLWithPath: args[2])
let tier = args.count >= 4 ? args[3] : "720"
let preset = (tier == "540") ? AVAssetExportPreset960x540 : AVAssetExportPreset1280x720

guard FileManager.default.fileExists(atPath: srcURL.path) else {
    fail(1, "input not found: \(srcURL.path)")
}
try? FileManager.default.removeItem(at: dstURL)

let asset = AVURLAsset(url: srcURL)
guard let session = AVAssetExportSession(asset: asset, presetName: preset) else {
    fail(2, "no export session for preset \(preset)")
}
session.outputURL = dstURL
session.outputFileType = .mp4
session.shouldOptimizeForNetworkUse = true  // faststart (moov first)

let sema = DispatchSemaphore(value: 0)
var exportError: Error?
var exportStatus: AVAssetExportSession.Status = .unknown
session.exportAsynchronously {
    exportStatus = session.status
    exportError = session.error
    sema.signal()
}
// AVAssetExportSession has no timeout; the Python caller enforces one.
sema.wait()

switch exportStatus {
case .completed:
    do {
        let attrs = try FileManager.default.attributesOfItem(atPath: dstURL.path)
        let size = (attrs[.size] as? NSNumber)?.intValue ?? 0
        if size <= 0 { fail(4, "empty output") }
        print(size)
    } catch {
        fail(4, "cannot stat output: \(error)")
    }
case .cancelled:
    fail(3, "export cancelled")
default:
    fail(3, "export failed: \(exportError?.localizedDescription ?? "unknown")")
}
