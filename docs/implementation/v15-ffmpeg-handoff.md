# 本地实际FFmpeg运行交接

已核验FFmpeg/ffprobe均为9.0.2 essentials_build。

```powershell
$taskFfmpegBin='C:\Users\Administrator\AppData\Local\Temp\juya-v15-ffmpeg\verified-20261011\ffmpeg-9.0.2-essentials_build\bin'
$env:PATH=$taskFfmpegBin+';'+$env:PATH
```

此赋值仅当前PowerShell进程及随后启动的子进程生效，不修改用户/机器PATH或.env。
Python common_env可用 `common_env['PATH']=str(ffmpeg_bin)+';'+common_env.get('PATH','')`。
若使用Settings显式路径，ffmpeg_path与ffprobe_path分别指向该目录下ffmpeg.exe、ffprobe.exe。
FFmpegSlicer独立运行须传绝对ffmpeg_path，inspect_media须传绝对ffprobe_path；
不要仅配置Settings却直接构造采用默认命令名的slicer而不传路径。

本仓私有SQL/音频门禁采用进程PATH注入实际执行解码及独立切片，未重新安装或改共享配置。
