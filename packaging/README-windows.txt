osv360 for Windows (experimental)
=================================

1. Install ffmpeg (once), e.g. in PowerShell:

       winget install Gyan.FFmpeg

   then sign out and back in (or reboot) so it is on your PATH.
   Alternatively put ffmpeg.exe and ffprobe.exe in a folder called "ffmpeg" next to osv360.exe.

2. Start osv360.exe. Windows SmartScreen may warn because the app is not code-signed:
   click "More info" -> "Run anyway".

3. Command line: use osv360-cli.exe, e.g.

       osv360-cli.exe convert C:\Videos\CAM_0001.OSV --output-dir C:\Videos\out
       osv360-cli.exe doctor

Hardware encoding uses NVIDIA NVENC, AMD AMF or Intel Quick Sync, whichever works;
otherwise the (slow) CPU encoder is used. See https://github.com/toccifrancesco/osv-slayer
