import requests
import json
import time
import logging
import sys
import os
from datetime import datetime
from typing import Optional, List, Dict
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

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
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
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
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.2f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.2f} PB"

    def create_check_report(self, servers_info: List[Dict],
                            high_traffic_servers: List[Dict],
                            processed_servers: List[Dict],
                            dry_run: bool = False) -> str:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode = "🧪 预览模式" if dry_run else "⚡ 实际执行"

        message = f"<b>🖥 Hetzner 服务器监控报告</b>\n"
        message += f"━━━━━━━━━━━━━━━━━━━━\n"
        message += f"🕐 检查时间: {timestamp}\n"
        message += f"📊 模式: {mode}\n"
        message += f"🔢 服务器总数: {len(servers_info)}\n"
        message += f"⚠️ 高流量服务器: {len(high_traffic_servers)}\n\n"

        message += f"<b>📋 服务器状态概览:</b>\n"
        message += f"━━━━━━━━━━━━━━━━━━━━\n"

        for server in servers_info:
            name = server['name']
            usage_percent = server['usage_percent']
            status = "🔴" if usage_percent > 0.8 else "🟡" if usage_percent > 0.6 else "🟢"
            message += f"\n{status} <b>{name}</b>\n"
            message += f"   └ 使用率: {usage_percent:.1%}\n"

        if processed_servers:
            message += f"\n<b>✅ 处理结果:</b>\n"
            message += f"━━━━━━━━━━━━━━━━━━━━\n"
            for server in processed_servers:
                icon = "✅" if server['success'] and not dry_run else "❌"
                status = "处理成功" if server['success'] else f"失败: {server.get('error', '未知错误')}"
                message += f"\n{icon} <b>{server['name']}</b>\n"
                message += f"   └ 状态: {status}\n"
                if 'new_ip' in server:
                    message += f"   └ IP: {server['new_ip']}\n"
        return message


class HetznerServerManager:
    def __init__(self, api_key: str, traffic_threshold: float = 0.8,
                 telegram_notifier: Optional[TelegramNotifier] = None,
                 ssh_keys: List[int] = None):
        self.api_key = api_key
        self.traffic_threshold = traffic_threshold
        self.telegram_notifier = telegram_notifier
        self.ssh_keys = ssh_keys or []
        self.base_url = "https://api.hetzner.cloud/v1"
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

    def get_servers(self) -> Optional[list]:
        try:
            response = requests.get(f"{self.base_url}/servers", headers=self.headers)
            response.raise_for_status()
            return response.json()['servers']
        except Exception as e:
            logging.error(f"获取服务器列表失败: {e}")
            return None

    def get_server_ipv4_id(self, server: dict) -> Optional[int]:
        if server.get('public_net') and server['public_net'].get('ipv4'):
            return server['public_net']['ipv4'].get('id')
        return None

    def wait_for_server_deletion(self, server_id: int, max_wait: int = 120) -> bool:
        logging.info(f"等待服务器 {server_id} 删除完成...")
        start_time = time.time()
        while time.time() - start_time < max_wait:
            response = requests.get(f"{self.base_url}/servers/{server_id}", headers=self.headers)
            if response.status_code == 404:
                logging.info("✓ 服务器已完全删除")
                return True
            time.sleep(5)
        return False

    def delete_server(self, server_id: int) -> bool:
        try:
            logging.info(f"正在删除服务器 {server_id}...")
            requests.delete(f"{self.base_url}/servers/{server_id}", headers=self.headers).raise_for_status()
            return self.wait_for_server_deletion(server_id)
        except Exception as e:
            logging.error(f"删除服务器时发生错误: {e}")
            return False

    def wait_for_ip_ready(self, ipv4_id: int, max_retries: int = 12) -> bool:
        """检查 Primary IP 是否已释放（变为未分配状态）"""
        logging.info(f"检查 IP (ID: {ipv4_id}) 是否已释放...")
        for i in range(max_retries):
            try:
                response = requests.get(f"{self.base_url}/primary_ips/{ipv4_id}", headers=self.headers)
                response.raise_for_status()
                data = response.json()

                # 如果 assignee_id 为 None，说明 IP 已经彻底释放
                if data['primary_ip']['assignee_id'] is None:
                    logging.info(f"✓ IP (ID: {ipv4_id}) 已就绪")
                    return True

                logging.info(f"  IP 仍处于占用状态，等待中... ({i + 1}/{max_retries})")
                time.sleep(5)  # 每 5 秒检查一次
            except Exception as e:
                logging.error(f"检查 IP 状态时出错: {e}")
                time.sleep(5)
        return False

    def create_server_from_snapshot(self, server_config: Dict, snapshot_id: int,
                                    ipv4_id: int, ipv4_ip: str) -> Optional[int]:
        """增强版：带 IP 检查和重试机制的创建方法"""

        # 步骤 1: 确保 IP 已经从旧服务器释放
        if not self.wait_for_ip_ready(ipv4_id):
            logging.error(f"✗ IP (ID: {ipv4_id}) 释放超时，无法继续创建")
            return None

        # 步骤 2: 构建 Payload
        payload = {
            "name": server_config['name'],
            "ssh_keys": self.ssh_keys,
            "location": 2,
            "image": int(snapshot_id),
            "server_type": 110,
            "firewalls": [],
            "public_net": {
                "enable_ipv4": True,
                "enable_ipv6": True,
                "ipv4": int(ipv4_id)
            },
            "start_after_create": True
        }

        # 步骤 3: 尝试创建（带 3 次 422 重试机制）
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                logging.info(f"正在尝试创建服务器 (尝试 {attempt + 1}/{max_attempts})...")
                response = requests.post(f"{self.base_url}/servers", headers=self.headers, json=payload)

                if response.status_code == 201:
                    data = response.json()
                    server_id = data['server']['id']
                    logging.info(f"✓ 新服务器创建成功 (ID: {server_id})")
                    return server_id

                # 如果遇到 422 错误，检查是否是 IP 占用
                if response.status_code == 422:
                    error_data = response.json()
                    if error_data.get('error', {}).get('code') == 'primary_ip_assigned':
                        logging.warning("⚠ API 报告 IP 仍被分配，增加额外等待时间...")
                        time.sleep(10)  # 额外等待 10 秒
                        continue  # 重试

                # 其他错误则直接抛出
                logging.error(f"✗ API 返回不可恢复错误: {response.text}")
                response.raise_for_status()

            except Exception as e:
                logging.error(f"创建请求异常: {e}")
                if attempt == max_attempts - 1:
                    return None
                time.sleep(5)

        return None

    def process_high_traffic_server(self, server: dict, dry_run: bool = False) -> Dict:
        server_id = server['id']
        server_name = server['name']
        snapshot_id = server['image']['id'] if server.get('image') and server['image']['type'] == 'snapshot' else None

        ipv4_id = self.get_server_ipv4_id(server)
        ipv4_ip = server['public_net']['ipv4']['ip'] if ipv4_id else 'N/A'

        if not snapshot_id or not ipv4_id:
            return {'name': server_name, 'success': False, 'error': '缺失快照或IP ID'}

        if dry_run:
            logging.info(f"[预览] 将重建服务器 {server_name}")
            return {'name': server_name, 'success': True, 'new_ip': ipv4_ip}

        # 1. 删除
        if not self.delete_server(server_id):
            return {'name': server_name, 'success': False, 'error': '删除失败'}

        # 2. 创建
        new_id = self.create_server_from_snapshot(server, snapshot_id, ipv4_id, ipv4_ip)
        if new_id:
            return {'name': server_name, 'success': True, 'new_server_id': new_id, 'new_ip': ipv4_ip}

        return {'name': server_name, 'success': False, 'error': '重建失败'}

    def check_and_process_servers(self, dry_run: bool = False):
        """检查所有服务器并回显进度"""
        servers = self.get_servers()
        if not servers:
            logging.error("无法获取服务器列表，请检查网络或 API Key")
            return

        print(f"\n🔍 [扫描中] 正在检查 {len(servers)} 台服务器的流量...")

        servers_info, high_traffic_servers, processed_servers = [], [], []

        for server in servers:
            outgoing = int(server.get('outgoing_traffic', 0))
            included = int(server.get('included_traffic', 1))
            usage = outgoing / included

            # 实时回显当前处理的服务器
            status_icon = "⚠️" if usage >= self.traffic_threshold else "✅"
            print(f"  {status_icon} {server['name']:<40} | 使用率: {usage:>6.1%}")

            server_info = {
                'name': server['name'],
                'usage_percent': usage,
                'outgoing_traffic': outgoing,
                'included_traffic': included
            }
            servers_info.append(server_info)

            if usage >= self.traffic_threshold:
                high_traffic_servers.append(server_info)
                # 处理超标服务器
                result = self.process_high_traffic_server(server, dry_run)
                processed_servers.append(result)

        # 发送通知逻辑保持不变...
        if self.telegram_notifier:
            try:
                report = self.telegram_notifier.create_check_report(
                    servers_info, high_traffic_servers, processed_servers, dry_run
                )
                self.telegram_notifier.send_message(report)
            except Exception as e:
                logging.error(f"发送通知失败: {e}")

    def run_monitor(self, interval: int):
        """持续监控模式 - 增加实时控制台回显"""
        logging.info(f"🚀 监控服务已启动，检查间隔: {interval}秒")

        while True:
            try:
                # 执行检查
                self.check_and_process_servers(dry_run=False)

                # 检查结束后的处理
                next_check_time = datetime.now().timestamp() + interval
                next_check_str = datetime.fromtimestamp(next_check_time).strftime('%H:%M:%S')

                print(f"\n{'=' * 40}")
                logging.info(f"✅ 本轮检查完成。下次检查时间: {next_check_str}")
                print(f"{'=' * 40}\n")

                # 倒计时逻辑
                for remaining in range(interval, 0, -1):
                    # 使用 \r 实现单行覆盖输出，不会刷屏
                    sys.stdout.write(f"\r⏳ 距离下一次扫描还有: {remaining:4d} 秒... (按 Ctrl+C 停止)")
                    sys.stdout.flush()
                    time.sleep(1)

                print("\n\n🔄 正在开始新一轮扫描...")

            except KeyboardInterrupt:
                print("\n\n🛑 监控服务已被用户手动停止")
                break
            except Exception as e:
                logging.error(f"❌ 监控运行中发生错误: {e}")
                logging.info("将在 60 秒后重试...")
                time.sleep(60)


def main():
    API_KEY = os.getenv('HETZNER_API_KEY')
    THRESHOLD = float(os.getenv('TRAFFIC_THRESHOLD', '0.8'))
    INTERVAL = int(os.getenv('CHECK_INTERVAL', '1800'))

    # 解析 SSH KEYS (例如环境变量里是 "103101822")
    ssh_keys_raw = os.getenv('HETZNER_SSH_KEYS', '')
    ssh_keys = [int(k.strip()) for k in ssh_keys_raw.split(',') if k.strip().isdigit()]

    tg_token = os.getenv('TELEGRAM_BOT_TOKEN')
    tg_id = os.getenv('TELEGRAM_CHAT_ID')
    notifier = TelegramNotifier(tg_token, tg_id) if tg_token and tg_id else None

    manager = HetznerServerManager(API_KEY, THRESHOLD, notifier, ssh_keys)

    print("\n1. 单次检查\n2. 持续监控\n3. 预览模式")
    choice = input("请选择: ").strip()

    if choice == "1":
        manager.check_and_process_servers(False)
    elif choice == "2":
        manager.run_monitor(INTERVAL)
    elif choice == "3":
        manager.check_and_process_servers(True)


if __name__ == "__main__":
    main()
