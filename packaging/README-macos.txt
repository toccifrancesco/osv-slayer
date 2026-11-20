osv360 for macOS (experimental, Apple Silicon)
=============================================

1. Drag osv360.app into Applications.

2. Install ffmpeg (once) with Homebrew:

       brew install ffmpeg

3. The app is not notarized by Apple. The first time, right-click osv360.app -> Open -> Open.
   If macOS says the app "is damaged", run in Terminal:

       xattr -dr com.apple.quarantine /Applications/osv360.app

4. Command line: /Applications/osv360.app/Contents/MacOS/osv360-cli convert CAM_0001.OSV

Hardware encoding uses VideoToolbox. See https://github.com/toccifrancesco/osv-slayer
