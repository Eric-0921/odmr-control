#!/usr/bin/env python3
"""测试 SMB100A 连接脚本"""

import sys
sys.path.insert(0, '.')

from instruments.smb100a import SMB100AController

# VISA 地址
ADDRESS = "USB::0x0AAD::0x0054::101623::INSTR"

print("=" * 60)
print("SMB100A 连接测试")
print("=" * 60)
print("地址: {}".format(ADDRESS))
print()

controller = SMB100AController()

try:
    print("[1] 创建 SMB100A 控制器成功")
    print("[2] 尝试连接到 {}...".format(ADDRESS))

    idn = controller.connect(ADDRESS)
    print("[3] 连接成功!")
    print("    *IDN? 响应: {}".format(idn))

    print("[4] 读取当前参数...")
    params = controller.read_parameters()
    print("    频率: {} Hz".format(params.cw_hz))
    print("    功率: {} dBm".format(params.power_dbm))
    print("    RF输出: {}".format(params.rf_output))

    print("[5] 关闭连接...")
    controller.close()
    print("[6] 连接已关闭")

except Exception as e:
    print("[错误] 连接失败: {}".format(e))
    import traceback
    traceback.print_exc()

print("=" * 60)
