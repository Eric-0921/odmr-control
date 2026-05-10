#!/usr/bin/env python3
"""测试 OE1022D 连接脚本"""

import sys
sys.path.insert(0, '.')

from instruments.oe1022d import OE1022DController
import serial

BAUDRATE = 921600

print("=" * 50)
print("OE1022D 连接测试")
print("=" * 50)

# 扫描可用端口
print("\n[1] 扫描可用 COM 端口...")
available = []
for port_num in range(1, 21):
    port = "COM{}".format(port_num)
    try:
        s = serial.Serial(port)
        s.close()
        available.append(port)
        print("    发现: {}".format(port))
    except Exception:
        pass

if not available:
    print("    未找到可用端口")
    sys.exit(1)

print("\n[2] 尝试连接 OE1022D...")

for port in available:
    print("\n    尝试端口: {}".format(port))
    controller = OE1022DController()
    try:
        controller.connect(port, BAUDRATE)
        print("    [OK] 串口打开成功")

        print("    发送 *IDND? ...")
        response = controller.exchange("*IDND?", wait_s=0.2)
        print("    *IDND? 响应: {}".format(repr(response)))

        if not response.strip():
            print("    发送 *IDN? (备用) ...")
            response = controller.exchange("*IDN?", wait_s=0.2)
            print("    *IDN? 响应: {}".format(repr(response)))

        controller.close()
        print("    连接已关闭")

        if response.strip():
            print("\n[成功] OE1022D 连接正常!")
            print("=" * 50)
            sys.exit(0)

    except Exception as e:
        print("    [失败] {}".format(e))
        try:
            controller.close()
        except:
            pass

print("\n[失败] 未找到 OE1022D 设备")
print("=" * 50)
