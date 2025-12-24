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
        """将字节转换为易读格式 (GB/TB)"""
        # Hetzner API 返回的是字节，通常很大
        gb = bytes_value / (1024**3)
        if gb >= 1024:
            return f"{gb/1024:.2f} TB"
        return f"{gb:.2f} GB"

    def create_check_report(self, servers_info: List[Dict],
                                high_traffic_servers: List[Dict],
                                processed_servers: List[Dict],
                                dry_run: bool = False) -> str:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            message = f"<b>🖥 Hetzner 服务器监控报告</b>\n"
            message += f"━━━━━━━━━━━━━━━━━━━━\n"
            message += f"🕐 检查时间: {timestamp}\n"
            message += f"⚠️ 高流量预警: {len(high_traffic_servers)} 台\n\n"

            message += f"<b>📋 实时流量概览:</b>\n"
            message += f"━━━━━━━━━━━━━━━━━━━━\n"

            for server in servers_info:
                name = server['name']
                usage = server['usage_percent']
                
                # 换算为 GB (Hetzner API 返回的是 Byte)
                out_gb = server['outgoing_traffic'] / (1024**3)
                inc_gb = server['included_traffic'] / (1024**3)
                
                # 状态图标逻辑
                if usage >= 0.8: status_icon = "🔴"
                elif usage >= 0.6: status_icon = "🟡"
                else: status_icon = "🟢"

                message += f"\n{status_icon} <b>{name}</b>\n"
                message += f"   └ 占比: <code>{usage:.2%}</code>\n"
                message += f"   └ 详情: <code>{out_gb:.2f}GB / {inc_gb:.2f}GB</code>\n"

            if processed_servers:
                message += f"\n<b>✅ 重建任务处理结果:</b>\n"
                message += f"━━━━━━━━━━━━━━━━━━━━\n"
                for s in processed_servers:
                    res = "成功" if s['success'] else "失败"
                    message += f"• {s['name']}: {res}\n"
                    if 'new_ip' in s:
                        message += f"  └ 新IP: <code>{s['new_ip']}</code>\n"
            
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

    def wait_for_ip_ready(self, ipv4_id: int, max_retries: int = 15) -> bool:
        """检查 Primary IP 是否已释放（变为未分配状态）"""
        logging.info(f"检查 IP (ID: {ipv4_id}) 释放状态...")
        for i in range(max_retries):
            try:
                response = requests.get(f"{self.base_url}/primary_ips/{ipv4_id}", headers=self.headers)
                data = response.json()
                if data['primary_ip']['assignee_id'] is None:
                    logging.info(f"✓ IP (ID: {ipv4_id}) 已彻底就绪")
                    return True
                sys.stdout.write(f"\r  └ IP 仍在占用，等待释放中... ({i+1}/{max_retries})")
                sys.stdout.flush()
                time.sleep(5)
            except Exception:
                time.sleep(5)
        return False

    def delete_server(self, server_id: int) -> bool:
        try:
            logging.info(f"正在向 API 发送删除指令: {server_id}...")
            requests.delete(f"{self.base_url}/servers/{server_id}", headers=self.headers).raise_for_status()
            # 轮询直到服务器对象消失
            for _ in range(24):
                response = requests.get(f"{self.base_url}/servers/{server_id}", headers=self.headers)
                if response.status_code == 404:
                    logging.info("✓ 服务器对象已从 Hetzner 系统中移除")
                    return True
                time.sleep(5)
            return False
        except Exception as e:
            logging.error(f"删除服务器异常: {e}")
            return False

    def create_server_from_snapshot(self, server_config: Dict, snapshot_id: int,
                                    ipv4_id: int) -> Optional[int]:
        """带重试机制的服务器创建"""
        if not self.wait_for_ip_ready(ipv4_id):
            return None

        payload = {
            "name": server_config['name'],
            "ssh_keys": self.ssh_keys,
            "location": 2, # nbg1
            "image": int(snapshot_id),
            "server_type": 110, # cpx32
            "firewalls": [],
            "public_net": {"enable_ipv4": True, "enable_ipv6": True, "ipv4": int(ipv4_id)},
            "start_after_create": True
        }

        for attempt in range(3):
            try:
                logging.info(f"尝试创建新服务器 (尝试 {attempt+1}/3)...")
                response = requests.post(f"{self.base_url}/servers", headers=self.headers, json=payload)
                if response.status_code == 201:
                    new_id = response.json()['server']['id']
                    logging.info(f"✓ 新服务器创建成功! ID: {new_id}")
                    return new_id
                
                # 如果依然报 IP 占用错误，增加等待后重试
                if response.status_code == 422 and "primary_ip_assigned" in response.text:
                    logging.warning("⚠ API 同步延迟: IP 仍显示被分配，等待 10s 后重试...")
                    time.sleep(10)
                    continue
                
                logging.error(f"✗ 创建失败，API 返回: {response.text}")
                break
            except Exception as e:
                logging.error(f"创建过程中断: {e}")
                time.sleep(5)
        return None

    def process_high_traffic_server(self, server: dict) -> Dict:
        name = server['name']
        snapshot_id = server['image']['id'] if server.get('image') and server['image']['type'] == 'snapshot' else None
        ipv4_id = server['public_net']['ipv4']['id'] if server.get('public_net') and server['public_net'].get('ipv4') else None

        if not snapshot_id or not ipv4_id:
            return {'name': name, 'success': False, 'error': '缺失必要 ID'}

        # 核心逻辑：先删除，后创建
        if self.delete_server(server['id']):
            new_id = self.create_server_from_snapshot(server, snapshot_id, ipv4_id)
            if new_id:
                return {'name': name, 'success': True, 'new_ip': server['public_net']['ipv4']['ip']}
        
        return {'name': name, 'success': False, 'error': '流程执行失败'}

    def check_and_process_servers(self, dry_run: bool = False):
            servers = self.get_servers()
            if not servers: return

            print(f"\n🔍 [开始扫描] 正在检查 {len(servers)} 台服务器的实时流量...")
            servers_info, high_traffic, processed = [], [], []

            for server in servers:
                # 获取原始字节数据
                outgoing = int(server.get('outgoing_traffic', 0))
                included = int(server.get('included_traffic', 1))
                usage = outgoing / included
                
                # 控制台回显
                status_icon = "⚠️" if usage >= self.traffic_threshold else "✅"
                print(f"  {status_icon} {server['name']:<40} | 使用率: {usage:>6.1%}")

                # 【关键改动】：将所有流量字段存入字典，传给 Telegram 生成报告
                info = {
                    'name': server['name'], 
                    'usage_percent': usage,
                    'outgoing_traffic': outgoing,   # 新增
                    'included_traffic': included    # 新增
                }
                servers_info.append(info)

                # 判定是否需要重建
                if usage >= self.traffic_threshold:
                    high_traffic.append(info)
                    # 执行处理并记录结果
                    result = self.process_high_traffic_server(server, dry_run)
                    processed.append(result)

            # 只要配置了机器人，每轮扫描结束都发报告
            if self.telegram_notifier:
                try:
                    # 传入所有搜集到的数据
                    report = self.telegram_notifier.create_check_report(
                        servers_info, 
                        high_traffic, 
                        processed, 
                        dry_run
                    )
                    self.telegram_notifier.send_message(report)
                except Exception as e:
                    logging.error(f"发送通知失败: {e}")


    def run_monitor(self, interval: int):
        """主运行循环"""
        logging.info(f"🚀 监控服务启动成功，当前检查间隔为 {interval} 秒")
        while True:
            try:
                self.check_and_process_servers()
                
                print(f"\n" + "="*45)
                logging.info(f"本轮扫描结束。")
                print("="*45)
                
                # 倒计时显示逻辑
                for remaining in range(interval, 0, -1):
                    sys.stdout.write(f"\r⏳ 下一次扫描倒计时: {remaining:4d} 秒... (按 Ctrl+C 停止服务)")
                    sys.stdout.flush()
                    time.sleep(1)
                print("\n\n🔄 正在唤醒扫描程序...")
                
            except KeyboardInterrupt:
                print("\n\n🛑 监控服务已安全停止。")
                break
            except Exception as e:
                logging.error(f"发生未预期错误: {e}")
                time.sleep(60)

def main():
    # 基础配置
    API_KEY = os.getenv('HETZNER_API_KEY')
    THRESHOLD = float(os.getenv('TRAFFIC_THRESHOLD', '0.8'))
    INTERVAL = int(os.getenv('CHECK_INTERVAL', '1800'))
    
    # 密钥配置
    keys_raw = os.getenv('HETZNER_SSH_KEYS', '')
    ssh_keys = [int(k.strip()) for k in keys_raw.split(',') if k.strip().isdigit()]

    # 通知配置
    tg_token = os.getenv('TELEGRAM_BOT_TOKEN')
    tg_id = os.getenv('TELEGRAM_CHAT_ID')
    notifier = TelegramNotifier(tg_token, tg_id) if tg_token and tg_id else None

    if not API_KEY:
        print("❌ 错误: 环境变量中未找到 HETZNER_API_KEY")
        return

    manager = HetznerServerManager(API_KEY, THRESHOLD, notifier, ssh_keys)
    
    # 直接启动监控
    manager.run_monitor(INTERVAL)

if __name__ == "__main__":
    main()
