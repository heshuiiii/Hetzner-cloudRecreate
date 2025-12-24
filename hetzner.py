import requests
import json
import time
import logging
import sys
from datetime import datetime
from typing import Optional, List, Dict

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('hetzner_monitor.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)


class TelegramNotifier:
    """Telegram 通知类"""

    def __init__(self, bot_token: str, chat_id: str):
        """
        初始化 Telegram 通知器

        Args:
            bot_token: Telegram Bot Token
            chat_id: Telegram Chat ID
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        发送 Telegram 消息

        Args:
            message: 消息内容
            parse_mode: 解析模式 (HTML 或 Markdown)

        Returns:
            是否发送成功
        """
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }

            response = requests.post(self.api_url, json=payload, timeout=10)
            response.raise_for_status()

            logging.info("✓ Telegram 通知发送成功")
            return True
        except Exception as e:
            logging.error(f"✗ Telegram 通知发送失败: {e}")
            return False

    def format_bytes(self, bytes_value: int) -> str:
        """格式化字节数为易读格式"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.2f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.2f} PB"

    def create_check_report(self, servers_info: List[Dict],
                            high_traffic_servers: List[Dict],
                            processed_servers: List[Dict],
                            dry_run: bool = False) -> str:
        """
        创建检查报告消息

        Args:
            servers_info: 所有服务器信息列表
            high_traffic_servers: 高流量服务器列表
            processed_servers: 已处理的服务器列表
            dry_run: 是否为测试模式
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode = "🧪 测试模式" if dry_run else "⚡ 实际执行"

        message = f"<b>🖥 Hetzner 服务器监控报告</b>\n"
        message += f"━━━━━━━━━━━━━━━━━━━━\n"
        message += f"🕐 检查时间: {timestamp}\n"
        message += f"📊 模式: {mode}\n"
        message += f"🔢 服务器总数: {len(servers_info)}\n"
        message += f"⚠️ 高流量服务器: {len(high_traffic_servers)}\n\n"

        # 所有服务器状态概览
        message += f"<b>📋 服务器状态概览:</b>\n"
        message += f"━━━━━━━━━━━━━━━━━━━━\n"

        for server in servers_info:
            name = server['name']
            usage_percent = server['usage_percent']
            outgoing = self.format_bytes(server['outgoing_traffic'])
            included = self.format_bytes(server['included_traffic'])

            if usage_percent > 0.8:
                status = "🔴"
            elif usage_percent > 0.6:
                status = "🟡"
            else:
                status = "🟢"

            message += f"\n{status} <b>{name}</b>\n"
            message += f"   └ 流量: {outgoing} / {included}\n"
            message += f"   └ 使用率: {usage_percent:.1%}\n"

        # 高流量服务器详细信息
        if high_traffic_servers:
            message += f"\n<b>🚨 高流量服务器详情:</b>\n"
            message += f"━━━━━━━━━━━━━━━━━━━━\n"

            for idx, server in enumerate(high_traffic_servers, 1):
                message += f"\n<b>#{idx} {server['name']}</b>\n"
                message += f"├ ID: {server['id']}\n"
                message += f"├ IPv4: {server['ipv4']}\n"
                message += f"├ 类型: {server['server_type']}\n"
                message += f"├ 数据中心: {server['datacenter']}\n"
                message += f"├ 镜像: {server['image']}\n"
                message += f"├ 流量使用: {self.format_bytes(server['outgoing_traffic'])}\n"
                message += f"├ 流量配额: {self.format_bytes(server['included_traffic'])}\n"
                message += f"└ 使用率: <b>{server['usage_percent']:.1%}</b>\n"

        # 处理结果
        if processed_servers:
            message += f"\n<b>✅ 处理结果:</b>\n"
            message += f"━━━━━━━━━━━━━━━━━━━━\n"

            for server in processed_servers:
                if server['success']:
                    icon = "✅" if not dry_run else "🧪"
                    status = "处理成功" if not dry_run else "测试完成(未实际执行)"
                else:
                    icon = "❌"
                    status = "处理失败"

                message += f"\n{icon} <b>{server['name']}</b>\n"
                message += f"   └ 状态: {status}\n"

                if dry_run and server['success']:
                    message += f"   └ 将执行:\n"
                    message += f"      • 创建快照\n"
                    message += f"      • 关闭并删除服务器\n"
                    message += f"      • 创建新服务器\n"
                    message += f"      • 使用快照恢复数据\n"
        else:
            message += f"\n✅ <b>所有服务器流量使用正常</b>\n"

        message += f"\n━━━━━━━━━━━━━━━━━━━━\n"
        message += f"💡 监控系统运行正常"

        return message


class HetznerServerManager:
    def __init__(self, api_key: str, traffic_threshold: float = 0.8,
                 telegram_notifier: Optional[TelegramNotifier] = None):
        """
        初始化 Hetzner 服务器管理器

        Args:
            api_key: Hetzner API 密钥
            traffic_threshold: 流量使用阈值(0-1之间),默认0.8即80%
            telegram_notifier: Telegram 通知器实例
        """
        self.api_key = api_key
        self.traffic_threshold = traffic_threshold
        self.telegram_notifier = telegram_notifier
        self.base_url = "https://api.hetzner.cloud/v1"
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

    def get_servers(self) -> Optional[list]:
        """获取所有服务器列表"""
        try:
            response = requests.get(
                f"{self.base_url}/servers",
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()['servers']
        except Exception as e:
            logging.error(f"获取服务器列表失败: {e}")
            return None

    def create_snapshot(self, server_id: int, server_name: str) -> Optional[int]:
        """创建服务器快照"""
        try:
            dt_string = datetime.now().strftime("%Y%m%d-%H%M%S")
            payload = {
                "description": f"{server_name}-{dt_string}",
                "labels": {"auto_snapshot": "true"},
                "type": "snapshot"
            }

            logging.info(f"正在为服务器 {server_name} 创建快照...")
            response = requests.post(
                f"{self.base_url}/servers/{server_id}/actions/create_image",
                headers=self.headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()

            # 等待快照完成
            time.sleep(150)

            if data['action'].get('error') is None:
                snap_id = data['image']['id']
                logging.info(f"✓ 快照创建成功 (ID: {snap_id})")
                return snap_id
            else:
                logging.error(f"✗ 快照创建失败: {data['action']['error']}")
                return None
        except Exception as e:
            logging.error(f"创建快照时发生错误: {e}")
            return None

    def power_off_server(self, server_id: int) -> bool:
        """关闭服务器"""
        try:
            logging.info(f"正在关闭服务器 {server_id}...")
            response = requests.post(
                f"{self.base_url}/servers/{server_id}/actions/poweroff",
                headers=self.headers
            )
            response.raise_for_status()
            data = response.json()

            time.sleep(10)

            if data['action'].get('error') is None:
                logging.info("✓ 服务器已关闭")
                return True
            else:
                logging.error(f"✗ 关闭服务器失败: {data['action']['error']}")
                return False
        except Exception as e:
            logging.error(f"关闭服务器时发生错误: {e}")
            return False

    def unassign_ip(self, ipv4_id: int) -> bool:
        """取消IP分配"""
        try:
            logging.info(f"正在取消IP分配 {ipv4_id}...")
            response = requests.post(
                f"{self.base_url}/primary_ips/{ipv4_id}/actions/unassign",
                headers=self.headers
            )
            response.raise_for_status()
            data = response.json()

            time.sleep(10)

            if data['action'].get('error') is None:
                logging.info("✓ IP已取消分配")
                return True
            else:
                logging.error(f"✗ 取消IP分配失败: {data['action']['error']}")
                return False
        except Exception as e:
            logging.error(f"取消IP分配时发生错误: {e}")
            return False

    def delete_server(self, server_id: int) -> bool:
        """删除服务器"""
        try:
            logging.info(f"正在删除服务器 {server_id}...")
            response = requests.delete(
                f"{self.base_url}/servers/{server_id}",
                headers=self.headers
            )
            response.raise_for_status()

            time.sleep(10)
            logging.info("✓ 服务器已删除")
            return True
        except Exception as e:
            logging.error(f"删除服务器时发生错误: {e}")
            return False

    def create_server(self, name: str, datacenter: str, server_type: str,
                      ipv4_id: int) -> Optional[int]:
        """创建新服务器"""
        try:
            payload = {
                "datacenter": datacenter,
                "image": "ubuntu-20.04",
                "name": name,
                "public_net": {
                    "enable_ipv4": True,
                    "enable_ipv6": False,
                    "ipv4": ipv4_id
                },
                "server_type": server_type,
                "start_after_create": True
            }

            logging.info(f"正在创建新服务器 {name}...")
            response = requests.post(
                f"{self.base_url}/servers",
                headers=self.headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()

            time.sleep(60)

            if data['action'].get('error') is None:
                server_id = data['server']['id']
                logging.info(f"✓ 新服务器创建成功 (ID: {server_id})")
                return server_id
            else:
                logging.error(f"✗ 创建服务器失败: {data['action']['error']}")
                return None
        except Exception as e:
            logging.error(f"创建服务器时发生错误: {e}")
            return None

    def rebuild_server(self, server_id: int, snap_id: int) -> bool:
        """使用快照重建服务器"""
        try:
            payload = {"image": str(snap_id)}

            logging.info(f"正在使用快照重建服务器 {server_id}...")
            response = requests.post(
                f"{self.base_url}/servers/{server_id}/actions/rebuild",
                headers=self.headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()

            if data['action'].get('error') is None:
                logging.info("✓ 服务器重建成功!")
                return True
            else:
                logging.error(f"✗ 重建服务器失败: {data['action']['error']}")
                return False
        except Exception as e:
            logging.error(f"重建服务器时发生错误: {e}")
            return False

    def process_high_traffic_server(self, server: dict, dry_run: bool = False) -> bool:
        """
        处理高流量服务器的完整流程

        Args:
            server: 服务器信息字典
            dry_run: 是否为测试模式(True=仅打印不执行, False=实际执行)
        """
        server_id = server['id']
        server_name = server['name']
        ipv4 = server['public_net']['ipv4']['ip']
        ipv4_id = server['public_net']['ipv4']['id']
        server_type = server['server_type']['name']
        datacenter = server['datacenter']['name']
        image = server['image']['name'] if server.get('image') else 'ubuntu-20.04'

        logging.info(f"\n{'=' * 60}")
        if dry_run:
            logging.info(f"[测试模式] 高流量服务器处理预览: {server_name}")
        else:
            logging.info(f"开始处理高流量服务器: {server_name}")
        logging.info(f"{'=' * 60}")

        # 打印服务器信息
        logging.info(f"\n当前服务器配置:")
        logging.info(f"  服务器ID: {server_id}")
        logging.info(f"  服务器名称: {server_name}")
        logging.info(f"  IPv4地址: {ipv4}")
        logging.info(f"  IPv4 ID: {ipv4_id}")
        logging.info(f"  服务器类型: {server_type}")
        logging.info(f"  数据中心: {datacenter}")
        logging.info(f"  当前镜像: {image}")

        if dry_run:
            logging.info(f"\n[测试模式] 将执行以下操作:")
            logging.info(f"  1. 创建快照: {server_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            logging.info(f"  2. 关闭服务器: {server_id}")
            logging.info(f"  3. 取消IP分配: {ipv4_id} ({ipv4})")
            logging.info(f"  4. 删除服务器: {server_id}")
            logging.info(f"  5. 创建新服务器配置:")
            logging.info(f"     - 名称: {server_name}")
            logging.info(f"     - 类型: {server_type}")
            logging.info(f"     - 数据中心: {datacenter}")
            logging.info(f"     - 镜像: ubuntu-20.04")
            logging.info(f"     - IPv4: {ipv4_id} ({ipv4})")
            logging.info(f"     - 启动: True")
            logging.info(f"  6. 使用快照重建新服务器")
            logging.info(f"\n[测试模式] 未实际执行任何操作")
            logging.info(f"{'=' * 60}\n")
            return True

        # 实际执行模式
        # 1. 创建快照
        snap_id = self.create_snapshot(server_id, server_name)
        if not snap_id:
            return False

        # 2. 关闭服务器
        if not self.power_off_server(server_id):
            return False

        # 3. 取消IP分配
        if not self.unassign_ip(ipv4_id):
            return False

        # 4. 删除服务器
        if not self.delete_server(server_id):
            return False

        # 5. 创建新服务器
        new_server_id = self.create_server(server_name, datacenter,
                                           server_type, ipv4_id)
        if not new_server_id:
            return False

        # 6. 使用快照重建
        if not self.rebuild_server(new_server_id, snap_id):
            return False

        logging.info(f"{'=' * 60}")
        logging.info(f"服务器 {server_name} 处理完成!")
        logging.info(f"{'=' * 60}\n")
        return True

    def check_and_process_servers(self, dry_run: bool = False):
        """
        检查所有服务器并处理高流量服务器

        Args:
            dry_run: 是否为测试模式(True=仅打印不执行, False=实际执行)
        """
        mode_text = "[测试模式] " if dry_run else ""
        logging.info(f"\n{mode_text}开始检查服务器流量使用情况...")

        servers = self.get_servers()
        if not servers:
            logging.error("无法获取服务器列表")
            return

        # 收集所有服务器信息
        servers_info = []
        high_traffic_servers = []
        processed_servers = []

        for server in servers:
            name = server['name']
            server_id = server['id']
            outgoing_traffic = int(server.get('outgoing_traffic', 0))
            included_traffic = int(server.get('included_traffic', 1))
            ipv4 = server['public_net']['ipv4']['ip']
            ipv4_id = server['public_net']['ipv4']['id']
            server_type = server['server_type']['name']
            datacenter = server['datacenter']['name']
            image = server['image']['name'] if server.get('image') else 'ubuntu-20.04'

            # 计算使用百分比
            percent_usage = outgoing_traffic / included_traffic if included_traffic > 0 else 0

            # 记录服务器信息
            server_info = {
                'id': server_id,
                'name': name,
                'ipv4': ipv4,
                'ipv4_id': ipv4_id,
                'server_type': server_type,
                'datacenter': datacenter,
                'image': image,
                'outgoing_traffic': outgoing_traffic,
                'included_traffic': included_traffic,
                'usage_percent': percent_usage
            }
            servers_info.append(server_info)

            logging.info(f"\n服务器: {name}")
            logging.info(f"  流量使用: {outgoing_traffic:,} / {included_traffic:,} bytes")
            logging.info(f"  使用率: {percent_usage:.1%}")

            if percent_usage > self.traffic_threshold:
                logging.warning(f"  ⚠ 流量使用超过阈值 {self.traffic_threshold:.0%}!")
                high_traffic_servers.append(server_info)

                # 处理服务器
                success = self.process_high_traffic_server(server, dry_run=dry_run)
                processed_servers.append({
                    'name': name,
                    'success': success
                })
            else:
                logging.info(f"  ✓ 流量使用正常")

        # 发送 Telegram 通知
        if self.telegram_notifier:
            try:
                message = self.telegram_notifier.create_check_report(
                    servers_info=servers_info,
                    high_traffic_servers=high_traffic_servers,
                    processed_servers=processed_servers,
                    dry_run=dry_run
                )
                self.telegram_notifier.send_message(message)
            except Exception as e:
                logging.error(f"发送 Telegram 通知时出错: {e}")

    def run_monitor(self, check_interval: int = 3600):
        """
        持续监控模式

        Args:
            check_interval: 检查间隔时间(秒),默认3600秒(1小时)
        """
        logging.info(f"启动监控服务,检查间隔: {check_interval}秒")

        while True:
            try:
                self.check_and_process_servers()
                logging.info(f"\n下次检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                             f"(等待 {check_interval} 秒)\n")
                time.sleep(check_interval)
            except KeyboardInterrupt:
                logging.info("\n监控服务已停止")
                break
            except Exception as e:
                logging.error(f"监控过程中发生错误: {e}")
                time.sleep(60)  # 发生错误时等待1分钟后重试


def main():
    # 配置参数
    API_KEY = ''
    TRAFFIC_THRESHOLD = 0.85  # 80% 流量阈值
    CHECK_INTERVAL = 1800  # 每小时检查一次

    # Telegram 配置
    TELEGRAM_BOT_TOKEN = ''
    TELEGRAM_CHAT_ID = ''

    # 创建 Telegram 通知器
    telegram_notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    # 测试 Telegram 连接
    logging.info("测试 Telegram 连接...")
    test_message = "🤖 <b>Hetzner 监控系统已启动</b>\n\n✅ Telegram 通知功能正常"
    if telegram_notifier.send_message(test_message):
        logging.info("✓ Telegram 连接测试成功")
    else:
        logging.warning("⚠ Telegram 连接测试失败,将继续运行但不发送通知")
        telegram_notifier = None

    # 创建管理器实例
    manager = HetznerServerManager(API_KEY, TRAFFIC_THRESHOLD, telegram_notifier)

    # 选择运行模式
    print("\n选择运行模式:")
    print("1. 单次检查(实际执行)")
    print("2. 持续监控(实际执行)")
    print("3. 测试模式(仅查看,不实际执行删除和创建)")
    choice = input("请输入选项 (1/2/3): ").strip()

    if choice == "1":
        # 单次检查 - 实际执行
        manager.check_and_process_servers(dry_run=False)
    elif choice == "2":
        # 持续监控 - 实际执行
        manager.run_monitor(CHECK_INTERVAL)
    elif choice == "3":
        # 测试模式 - 仅打印不执行
        print("\n" + "=" * 60)
        print("测试模式: 将显示所有要执行的操作,但不会实际执行")
        print("=" * 60 + "\n")
        manager.check_and_process_servers(dry_run=True)
    else:
        print("无效选项")


if __name__ == "__main__":

    main()

