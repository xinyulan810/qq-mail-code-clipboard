@echo off
cd /d "%~dp0"

echo 正在安装打包工具（PyInstaller）...
python -m pip install pyinstaller -i https://mirrors.aliyun.com/pypi/simple/ --disable-pip-version-check -q

echo 正在生成图标...
python -c "import gui; gui._make_tray_icon_image(256).save('icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

echo 正在打包（约 1-2 分钟）...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "QQMailCodeTool" --icon icon.ico app.py

echo.
echo 打包完成：dist\QQMailCodeTool.exe
pause
