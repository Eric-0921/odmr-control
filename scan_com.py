import serial.tools.list_ports

print("=" * 50)
print("扫描可用 COM 端口")
print("=" * 50)

ports = list(serial.tools.list_ports.comports())
if ports:
    for p in ports:
        print(f"  {p.device} - {p.description}")
else:
    print("  未找到串口")
print("=" * 50)
