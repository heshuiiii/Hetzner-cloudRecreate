import requests
import json
import time
import logging
import sys
import os
from datetime import datetime, time as dt_time
from typing import Optional, List, Dict, Set
from dotenv import load_dotenv
from flask import Flask, jsonify
from threading import Thread

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


class TimeWindowManager:
    """时间窗口管理器 - 支持跨午夜的时间段"""
    def __init__(self, start_hour: int = 8, end_hour: int = 23, end_minute: int = 30):
        self.start_time = dt_time(start_hour, 0)
        self.end_time = dt_time(end_hour, end_minute)
        self.servers_deleted = False
        self.servers_created = False
        self.crosses_midnight = start_hour > end_hour
        
    def is_in_work_window(self) -> bool:
        """判断当前是否在工作时段（支持跨午夜）"""
        now = datetime.now().time()
        
        if self.crosses_midnight:
            return now >= self.start_time or now <= self.end_time
        else:
            return self.start_time <= now <= self.end_time
    
    def should_delete_servers(self) -> bool:
        """判断是否应该删除服务器（刚过结束时间且未删除）"""
        now = datetime.now().time()
        
        if self.servers_deleted:
            return False
        
        if self.crosses_midnight:
            is_after_end = now > self.end_time
            is_before_start = now < self.start_time
            return is_after_end and is_before_start
        else:
            return now > self.end_time
    
    def should_create_initial_servers(self) -> bool:
        """判断是否应该创建初始服务器（刚进入工作时段且未创建）"""
        return self.is_in_work_window() and not self.servers_created
    
    def reset_flags_on_work_start(self):
        """重置标记（重新进入工作时段时）"""
        if self.is_in_work_window():
            if self.servers_deleted:
                logging.info("🌅 进入新的工作时段，重置删除标记")
                self.servers_deleted = False
                self.servers_created = False
    
    def mark_as_deleted(self):
        """标记服务器已删除"""
        self.servers_deleted = True
        self.servers_created = False
    
    def mark_as_created(self):
        """标记初始服务器已创建"""
        self.servers_created = True
    
    def get_status_info(self) -> str:
        """获取当前状态信息"""
        now = datetime.now()
        current_time = now.strftime("%H:%M:%S")
        
        if self.is_in_work_window():
            status = "工作时段 ✓"
        else:
            status = "非工作时段 (服务器已删除)" if self.servers_deleted else "非工作时段 (等待删除)"
        
        time_range = f"{self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"
        if self.crosses_midnight:
            time_range += " (跨午夜)"
        
        return f"当前: {current_time} | 工作时段: {time_range} | 状态: {status}"


class DownloaderAPI:
    """下载器 API 管理器 - 智能IP负载均衡"""
    def __init__(self, base_url: str, cookies: str):
        self.base_url = base_url.rstrip('/')
        self.cookies = cookies
        self.headers = {
            'Content-Type': 'application/json',
            'Cookie': cookies
        }
    
    def get_hetzner_downloaders(self) -> List[Dict]:
        """获取所有 Hetzner 相关的下载器"""
        try:
            response = requests.get(
                f"{self.base_url}/api/downloader/list",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            response_data = response.json()
            
            if isinstance(response_data, dict) and 'data' in response_data:
                all_downloaders = response_data['data']
            else:
                all_downloaders = response_data
            
            hetzner_downloaders = [
                d for d in all_downloaders 
                if isinstance(d, dict) and 'Hetzner' in d.get('alias', '')
            ]
            
            logging.info(f"✓ 获取到 {len(hetzner_downloaders)} 个 Hetzner 下载器")
            return hetzner_downloaders
            
        except Exception as e:
            logging.error(f"✗ 获取下载器列表失败: {e}")
            return []
    
    def extract_ip_from_url(self, url: str) -> Optional[str]:
        """从 URL 中提取 IP 地址"""
        import re
        match = re.search(r'(\d+\.\d+\.\d+\.\d+)', url)
        return match.group(1) if match else None
    
    def update_downloader_ip(self, downloader: Dict, new_ip: str) -> bool:
        """更新下载器的 IP 地址"""
        downloader_alias = downloader.get('alias', 'Unknown')
        old_url = downloader.get('clientUrl', '')
        
        if not old_url:
            logging.warning(f"⚠ 下载器 {downloader_alias} 没有 clientUrl")
            return False
        
        old_ip = self.extract_ip_from_url(old_url)
        if not old_ip:
            logging.warning(f"⚠ 无法从 URL {old_url} 中提取 IP")
            return False
        
        new_url = old_url.replace(old_ip, new_ip)
        downloader['clientUrl'] = new_url
        
        try:
            logging.info(f"📝 更新下载器 {downloader_alias}:")
            logging.info(f"   {old_ip} → {new_ip}")
            
            response = requests.post(
                f"{self.base_url}/api/downloader/modify",
                headers=self.headers,
                json=downloader,
                timeout=10
            )
            response.raise_for_status()
            
            logging.info(f"✓ 下载器 {downloader_alias} IP 已更新")
            return True
            
        except Exception as e:
            logging.error(f"✗ 更新下载器 {downloader_alias} 失败: {e}")
            return False
    
    def sync_downloaders_with_servers(self, server_ips: List[str]) -> Dict[str, int]:
        """同步下载器IP到服务器IP列表 - 强制负载均衡版"""
        if not server_ips:
            logging.warning("⚠ 没有可用的服务器 IP，跳过同步")
            return {'updated': 0, 'kept': 0, 'failed': 0}
        
        downloaders = self.get_hetzner_downloaders()
        if not downloaders:
            logging.warning("⚠ 未获取到任何 Hetzner 下载器")
            return {'updated': 0, 'kept': 0, 'failed': 0}
        
        logging.info(f"🔍 开始同步下载器IP，当前服务器IP: {', '.join(server_ips)}")
        
        from collections import Counter
        current_ips = {}
        ip_counter = Counter()
        
        for downloader in downloaders:
            alias = downloader.get('alias', 'Unknown')
            current_url = downloader.get('clientUrl', '')
            current_ip = self.extract_ip_from_url(current_url)
            
            if current_ip:
                current_ips[alias] = current_ip
                ip_counter[current_ip] += 1
        
        duplicate_ips = {ip for ip, count in ip_counter.items() if count > 1}
        if duplicate_ips:
            logging.warning(f"⚠ 检测到IP冲突: {', '.join(duplicate_ips)} 被多个下载器使用")
        
        available_ips = server_ips.copy()
        assignment = {}
        
        for alias, current_ip in current_ips.items():
            if current_ip in server_ips and current_ip not in duplicate_ips:
                assignment[alias] = current_ip
                if current_ip in available_ips:
                    available_ips.remove(current_ip)
                logging.info(f"✓ 下载器 {alias} ({current_ip}) 保持现有IP（无冲突）")
        
        for downloader in downloaders:
            alias = downloader.get('alias', 'Unknown')
            
            if alias in assignment:
                continue
            
            current_ip = current_ips.get(alias)
            
            if available_ips:
                target_ip = available_ips.pop(0)
            else:
                target_ip = server_ips[len(assignment) % len(server_ips)]
            
            assignment[alias] = target_ip
            
            if current_ip:
                if current_ip in duplicate_ips:
                    logging.info(f"⚠ 下载器 {alias} ({current_ip}) 有IP冲突，更新为 {target_ip}")
                elif current_ip not in server_ips:
                    logging.info(f"⚠ 下载器 {alias} ({current_ip}) 未指向现有服务器，更新为 {target_ip}")
                else:
                    logging.info(f"⚠ 下载器 {alias} ({current_ip}) 分配为 {target_ip}")
            else:
                logging.info(f"⚠ 下载器 {alias} 无IP，分配为 {target_ip}")
        
        updated = 0
        kept = 0
        failed = 0
        
        for downloader in downloaders:
            alias = downloader.get('alias', 'Unknown')
            target_ip = assignment.get(alias)
            current_ip = current_ips.get(alias)
            
            if not target_ip:
                failed += 1
                continue
            
            if current_ip == target_ip:
                kept += 1
            else:
                if self.update_downloader_ip(downloader, target_ip):
                    updated += 1
                else:
                    failed += 1
        
        logging.info(f"📊 下载器同步完成: 更新 {updated} 个, 保持 {kept} 个, 失败 {failed} 个")
        
        logging.info(f"📋 最终IP分配方案:")
        for alias, ip in assignment.items():
            logging.info(f"   • {alias}: {ip}")
        
        return {
            'updated': updated,
            'kept': kept,
            'failed': failed
        }


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

    def create_check_report(self, servers_info: List[Dict],
                                high_traffic_servers: List[Dict],
                                processed_servers: List[Dict],
                                time_window_info: str = "",
                                dry_run: bool = False,
                                initial_creation: bool = False) -> str:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            message = f"<b>🖥 Hetzner 服务器监控报告</b>\n"
            message += f"━━━━━━━━━━━━━━━━━━━━\n"
            message += f"🕐 检查时间: {timestamp}\n"
            if time_window_info:
                message += f"⏰ {time_window_info}\n"
            
            if initial_creation:
                message += f"🌅 类型: 工作时段初始化\n"
            else:
                message += f"⚠️ 高流量预警: {len(high_traffic_servers)} 台\n"
            
            if dry_run:
                message += f"🔍 模式: 仅检测 (未执行重建)\n"
            message += "\n"

            if servers_info:
                message += f"<b>📋 实时流量概览:</b>\n"
                message += f"━━━━━━━━━━━━━━━━━━━━\n"

                for server in servers_info:
                    name = server['name']
                    usage = server['usage_percent']
                    out_gb = server['outgoing_traffic'] / (1024**3)
                    inc_gb = server['included_traffic'] / (1024**3)
                    
                    if usage >= 0.8: 
                        status_icon = "🔴"
                    elif usage >= 0.6: 
                        status_icon = "🟡"
                    else: 
                        status_icon = "🟢"

                    message += f"\n{status_icon} <b>{name}</b>\n"
                    message += f"   └ 占比: <code>{usage:.2%}</code>\n"
                    message += f"   └ 详情: <code>{out_gb:.2f}GB / {inc_gb:.2f}GB</code>\n"

            if processed_servers:
                message += f"\n<b>✅ 处理结果:</b>\n"
                message += f"━━━━━━━━━━━━━━━━━━━━\n"
                for s in processed_servers:
                    res = "成功 ✓" if s['success'] else "失败 ✗"
                    message += f"• {s['name']}: {res}\n"
                    if 'server_type' in s:
                        message += f"  └ 类型: <code>{s['server_type']}</code>\n"
                    if 'new_ip' in s:
                        message += f"  └ 新IP: <code>{s['new_ip']}</code>\n"
                    if 'downloader_sync' in s:
                        message += f"  └ 下载器: {s['downloader_sync']}\n"
                    if 'error' in s:
                        message += f"  └ 原因: {s['error']}\n"
            
            return message


class HetznerServerManager:
    def __init__(self, api_key: str, traffic_threshold: float = 0.8,
                 telegram_notifier: Optional[TelegramNotifier] = None,
                 downloader_api: Optional[DownloaderAPI] = None,
                 time_window: Optional[TimeWindowManager] = None,
                 ssh_keys: List[int] = None,
                 server_types: List[int] = None,
                 max_servers: int = 0,
                 initial_snapshot_id: int = None,
                 qb_loadbalancer_url: str = None):  # 🆕 qBittorrent负载均衡器URL
        self.api_key = api_key
        self.traffic_threshold = traffic_threshold
        self.telegram_notifier = telegram_notifier
        self.downloader_api = downloader_api
        self.time_window = time_window
        self.ssh_keys = ssh_keys or []
        self.server_types = server_types or [116, 110, 117]
        self.max_servers = max_servers
        self.initial_snapshot_id = initial_snapshot_id
        self.qb_loadbalancer_url = qb_loadbalancer_url  # 🆕 保存负载均衡器URL
        self.base_url = "https://api.hetzner.cloud/v1"
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        self.server_type_names = {
            116: "cx43",
            110: "cpx22",
            117: "cx53",
            109: "cpx32"
        }

    def get_servers(self) -> Optional[list]:
        try:
            response = requests.get(f"{self.base_url}/servers", headers=self.headers)
            response.raise_for_status()
            return response.json()['servers']
        except Exception as e:
            logging.error(f"获取服务器列表失败: {e}")
            return None

    def get_server_ips(self) -> List[str]:
        """🆕 获取所有正在运行的服务器IP地址"""
        servers = self.get_servers()
        if not servers:
            return []
        
        ips = []
        for server in servers:
            # 检查服务器状态是否为运行中
            if server.get('status') == 'running':
                # 获取 IPv4 地址
                if server.get('public_net') and server['public_net'].get('ipv4'):
                    ip = server['public_net']['ipv4']['ip']
                    ips.append(ip)
        
        return ips

    def delete_server(self, server_id: int) -> bool:
        """删除服务器"""
        try:
            logging.info(f"正在删除服务器: {server_id}...")
            requests.delete(f"{self.base_url}/servers/{server_id}", headers=self.headers).raise_for_status()
            
            for _ in range(24):
                response = requests.get(f"{self.base_url}/servers/{server_id}", headers=self.headers)
                if response.status_code == 404:
                    logging.info("✓ 服务器已删除")
                    return True
                time.sleep(5)
            return False
        except Exception as e:
            logging.error(f"删除服务器异常: {e}")
            return False

    def _notify_loadbalancer_new_ip(self, new_ip: str) -> bool:
        """🆕 通知负载均衡器新IP（用于初始创建）"""
        if not self.qb_loadbalancer_url:
            return False
        
        try:
            url = f"{self.qb_loadbalancer_url.rstrip('/')}/api/update-ip"
            payload = {
                'new_ip': new_ip,
                'timestamp': datetime.now().isoformat()
            }
            
            logging.info(f"📡 通知负载均衡器新IP: {new_ip}")
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            
            result = response.json()
            if result.get('success'):
                logging.info(f"✓ 负载均衡器已接收新IP")
                return True
            else:
                logging.warning(f"⚠ 负载均衡器处理失败: {result.get('message')}")
                return False
                
        except Exception as e:
            logging.error(f"✗ 通知负载均衡器失败: {e}")
            return False

    def create_server_with_types(self, server_config: Dict, snapshot_id: int) -> Optional[Dict]:
        """尝试使用多种服务器类型创建服务器(不指定IP)"""
        for server_type_id in self.server_types:
            server_type_name = self.server_type_names.get(server_type_id, f"type_{server_type_id}")
            
            payload = {
                "name": server_config['name'],
                "ssh_keys": self.ssh_keys,
                "location": 2,
                "image": int(snapshot_id),
                "server_type": server_type_id,
                "firewalls": [],
                "public_net": {"enable_ipv4": True, "enable_ipv6": True},
                "start_after_create": True
            }

            for attempt in range(3):
                try:
                    logging.info(f"尝试创建 {server_type_name} 服务器 (尝试 {attempt+1}/3)...")
                    response = requests.post(f"{self.base_url}/servers", headers=self.headers, json=payload)
                    
                    if response.status_code == 201:
                        result = response.json()
                        new_id = result['server']['id']
                        actual_type = result['server']['server_type']['name']
                        new_ip = result['server']['public_net']['ipv4']['ip']
                        
                        logging.info(f"✓ 新服务器创建成功! ID: {new_id}, 类型: {actual_type}, IP: {new_ip}")
                        return {
                            'id': new_id,
                            'server_type': actual_type,
                            'new_ip': new_ip
                        }
                    
                    try:
                        error_data = response.json()
                        if 'error' in error_data:
                            error_msg = error_data['error'].get('message', '未知错误')
                            logging.warning(f"✗ {server_type_name} 创建失败: {error_msg}")
                            break
                    except:
                        logging.error(f"✗ 创建失败: {response.text}")
                        break
                        
                except Exception as e:
                    logging.error(f"创建过程中断: {e}")
                    time.sleep(5)
            
            logging.info(f"→ {server_type_name} 不可用，尝试下一个类型...")
        
        logging.error("✗ 所有服务器类型都创建失败")
        return None

    def rebuild_server(self, server: dict) -> Dict:
            """重建服务器 - 核心流程"""
            name = server['name']
            old_ip = server['public_net']['ipv4']['ip'] if server.get('public_net') and server['public_net'].get('ipv4') else None
            snapshot_id = server['image']['id'] if server.get('image') and server['image']['type'] == 'snapshot' else None

            if not snapshot_id:
                return {'name': name, 'success': False, 'error': '缺失快照ID'}

            if not self.delete_server(server['id']):
                return {'name': name, 'success': False, 'error': '删除失败'}
            
            time.sleep(10)
            
            result = self.create_server_with_types(server, snapshot_id)
            if not result:
                return {'name': name, 'success': False, 'error': '创建失败'}
            
            new_ip = result['new_ip']
            
            # 🆕 通知负载均衡器IP变更
            if self.qb_loadbalancer_url and old_ip and new_ip:
                self._notify_loadbalancer_ip_change(old_ip, new_ip)
            
            return {
                'name': name,
                'success': True,
                'new_ip': new_ip,
                'old_ip': old_ip,
                'server_type': result['server_type']
            }

    def _notify_loadbalancer_ip_change(self, old_ip: str, new_ip: str) -> bool:
        """🆕 通知负载均衡器IP已变更"""
        if not self.qb_loadbalancer_url:
            return False
        
        try:
            url = f"{self.qb_loadbalancer_url.rstrip('/')}/api/update-ip"
            payload = {
                'old_ip': old_ip,
                'new_ip': new_ip,
                'timestamp': datetime.now().isoformat()
            }
            
            logging.info(f"📡 通知负载均衡器: {old_ip} → {new_ip}")
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            
            result = response.json()
            if result.get('success'):
                updated_count = result.get('updated_count', 0)
                logging.info(f"✓ 负载均衡器已更新 {updated_count} 个实例")
                return True
            else:
                logging.warning(f"⚠ 负载均衡器更新失败: {result.get('message')}")
                return False
                
        except Exception as e:
            logging.error(f"✗ 通知负载均衡器失败: {e}")
            return False

    def create_initial_servers(self, count: int) -> List[Dict]:
        """创建初始服务器（工作时段开始时）"""
        if not self.initial_snapshot_id:
            logging.error("✗ 未配置初始快照ID，无法创建服务器")
            return []
        
        logging.info(f"🌅 开始创建 {count} 台初始服务器...")
        created = []
        
        for i in range(1, count + 1):
            server_config = {
                'name': f'hetzner-server-{i:02d}'
            }
            
            result = self.create_server_with_types(server_config, self.initial_snapshot_id)
            
            if result:
                created.append({
                    'name': server_config['name'],
                    'success': True,
                    'new_ip': result['new_ip'],
                    'server_type': result['server_type']
                })
                
                # 🆕 每创建一个服务器就立即通知负载均衡器
                if self.qb_loadbalancer_url:
                    self._notify_loadbalancer_new_ip(result['new_ip'])
                
                logging.info(f"✓ 初始服务器 {i}/{count} 创建成功")
            else:
                created.append({
                    'name': server_config['name'],
                    'success': False,
                    'error': '创建失败'
                })
                logging.error(f"✗ 初始服务器 {i}/{count} 创建失败")
            
            if i < count:
                time.sleep(5)
        
        return created

    def delete_all_servers_for_night(self) -> List[Dict]:
        """夜间模式：删除所有服务器"""
        servers = self.get_servers()
        if not servers:
            return []
        
        deleted = []
        logging.info(f"🌙 进入夜间模式，准备删除 {len(servers)} 台服务器...")
        
        for server in servers:
            if self.delete_server(server['id']):
                deleted.append({
                    'name': server['name'],
                    'success': True,
                    'action': '夜间删除'
                })
            else:
                deleted.append({
                    'name': server['name'],
                    'success': False,
                    'error': '删除失败'
                })
        
        return deleted

    def should_rebuild_more_servers(self, current_count: int) -> bool:
        """判断是否应该继续重建服务器"""
        if self.max_servers == 0:
            return True
        return current_count < self.max_servers

    def check_and_process_servers(self):
        """检查并处理服务器 - 主逻辑"""
        if self.time_window:
            self.time_window.reset_flags_on_work_start()
            
            status = self.time_window.get_status_info()
            logging.info(status)
            
            if self.time_window.should_create_initial_servers():
                servers = self.get_servers()
                if not servers or len(servers) == 0:
                    logging.info("🌅 检测到无服务器，开始创建初始服务器...")
                    
                    target_count = self.max_servers if self.max_servers > 0 else 3
                    created = self.create_initial_servers(target_count)
                    
                    self.time_window.mark_as_created()
                    
                    if self.downloader_api and created:
                        current_servers = self.get_servers()
                        if current_servers:
                            server_ips = [
                                s['public_net']['ipv4']['ip'] 
                                for s in current_servers 
                                if s.get('public_net') and s['public_net'].get('ipv4')
                            ]
                            
                            if server_ips:
                                logging.info(f"🔄 开始同步下载器IP...")
                                sync_result = self.downloader_api.sync_downloaders_with_servers(server_ips)
                                
                                for result in created:
                                    if result.get('success'):
                                        result['downloader_sync'] = f"更新 {sync_result['updated']} 个"
                    
                    if self.telegram_notifier:
                        time_info = ""
                        if self.time_window:
                            time_range = f"{self.time_window.start_time.strftime('%H:%M')}-{self.time_window.end_time.strftime('%H:%M')}"
                            if self.time_window.crosses_midnight:
                                time_range += " (跨午夜)"
                            time_info = f"工作时段: {time_range}"
                        
                        report = self.telegram_notifier.create_check_report(
                            [],
                            [],
                            created,
                            time_info,
                            initial_creation=True
                        )
                        self.telegram_notifier.send_message(report)
                    
                    return
            
            if self.time_window.should_delete_servers():
                deleted = self.delete_all_servers_for_night()
                self.time_window.mark_as_deleted()
                
                if self.telegram_notifier:
                    msg = f"<b>🌙 夜间模式启动</b>\n"
                    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
                    msg += f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    msg += f"已删除 {len(deleted)} 台服务器\n"
                    msg += f"工作时段: {self.time_window.start_time.strftime('%H:%M')}-{self.time_window.end_time.strftime('%H:%M')}\n"
                    if self.time_window.crosses_midnight:
                        msg += f"⚠️ 跨午夜时段"
                    self.telegram_notifier.send_message(msg)
                return
            
            if not self.time_window.is_in_work_window():
                logging.info("⏸ 非工作时段，跳过检查")
                return

        servers = self.get_servers()
        if not servers: 
            return

        print(f"\n🔍 [开始扫描] 正在检查 {len(servers)} 台服务器的实时流量...")
        if self.max_servers > 0:
            print(f"📊 [数量限制] 最多维持 {self.max_servers} 台服务器")
        
        servers_info, high_traffic, processed = [], [], []
        rebuilt_count = 0

        for server in servers:
            outgoing = int(server.get('outgoing_traffic', 0))
            included = int(server.get('included_traffic', 1))
            usage = outgoing / included
            
            status_icon = "⚠️" if usage >= self.traffic_threshold else "✅"
            print(f"  {status_icon} {server['name']:<40} | 使用率: {usage:>6.1%}")

            info = {
                'name': server['name'], 
                'usage_percent': usage,
                'outgoing_traffic': outgoing,
                'included_traffic': included
            }
            servers_info.append(info)

            if usage >= self.traffic_threshold:
                high_traffic.append(info)
                
                if self.should_rebuild_more_servers(rebuilt_count):
                    result = self.rebuild_server(server)
                    processed.append(result)
                    if result['success']:
                        rebuilt_count += 1
                else:
                    logging.info(f"⊘ 已达到服务器数量限制 ({self.max_servers})，跳过 {server['name']}")
                    processed.append({
                        'name': server['name'],
                        'success': False,
                        'error': f'已达数量限制 ({self.max_servers})'
                    })

        if self.downloader_api and processed:
            current_servers = self.get_servers()
            if current_servers:
                server_ips = [
                    s['public_net']['ipv4']['ip'] 
                    for s in current_servers 
                    if s.get('public_net') and s['public_net'].get('ipv4')
                ]
                
                if server_ips:
                    logging.info(f"🔄 开始同步下载器IP到服务器列表...")
                    sync_result = self.downloader_api.sync_downloaders_with_servers(server_ips)
                    
                    if sync_result['updated'] > 0:
                        for result in processed:
                            if result.get('success'):
                                result['downloader_sync'] = f"更新 {sync_result['updated']} 个"

        if self.telegram_notifier:
            try:
                time_info = ""
                if self.time_window:
                    time_range = f"{self.time_window.start_time.strftime('%H:%M')}-{self.time_window.end_time.strftime('%H:%M')}"
                    if self.time_window.crosses_midnight:
                        time_range += " (跨午夜)"
                    time_info = f"工作时段: {time_range}"
                
                report = self.telegram_notifier.create_check_report(
                    servers_info, 
                    high_traffic, 
                    processed,
                    time_info
                )
                self.telegram_notifier.send_message(report)
            except Exception as e:
                logging.error(f"发送通知失败: {e}")

    def run_monitor(self, interval: int):
        """主运行循环"""
        logging.info(f"🚀 监控服务启动成功，检查间隔: {interval} 秒")
        logging.info(f"📋 服务器类型优先级: {' > '.join([self.server_type_names.get(t, str(t)) for t in self.server_types])}")
        
        if self.max_servers > 0:
            logging.info(f"📊 服务器数量限制: 最多 {self.max_servers} 台")
        
        if self.time_window:
            time_range = f"{self.time_window.start_time.strftime('%H:%M')}-{self.time_window.end_time.strftime('%H:%M')}"
            if self.time_window.crosses_midnight:
                time_range += " (跨午夜)"
            logging.info(f"⏰ 工作时段: {time_range}")
        
        if self.initial_snapshot_id:
            logging.info(f"💾 初始快照ID: {self.initial_snapshot_id}")
        
        while True:
            try:
                self.check_and_process_servers()
                
                print(f"\n" + "="*45)
                logging.info(f"本轮扫描结束。")
                print("="*45)
                
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


# 🆕 Flask API 服务
app = Flask(__name__)
manager_instance = None  # 全局管理器实例


@app.route('/api/servers/ips', methods=['GET'])
def get_server_ips():
    """API端点：获取所有正在运行的服务器IP"""
    if not manager_instance:
        return jsonify({
            'success': False,
            'error': 'Server manager not initialized'
        }), 500
    
    try:
        ips = manager_instance.get_server_ips()
        return jsonify({
            'success': True,
            'count': len(ips),
            'ips': ips,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logging.error(f"API错误: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/servers/status', methods=['GET'])
def get_servers_status():
    """API端点：获取服务器详细状态"""
    if not manager_instance:
        return jsonify({
            'success': False,
            'error': 'Server manager not initialized'
        }), 500
    
    try:
        servers = manager_instance.get_servers()
        if not servers:
            return jsonify({
                'success': True,
                'count': 0,
                'servers': []
            })
        
        server_list = []
        for server in servers:
            server_info = {
                'id': server.get('id'),
                'name': server.get('name'),
                'status': server.get('status'),
                'ip': server['public_net']['ipv4']['ip'] if server.get('public_net') and server['public_net'].get('ipv4') else None,
                'server_type': server.get('server_type', {}).get('name'),
                'location': server.get('datacenter', {}).get('name'),
                'created': server.get('created')
            }
            server_list.append(server_info)
        
        return jsonify({
            'success': True,
            'count': len(server_list),
            'servers': server_list,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logging.error(f"API错误: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def run_flask_api(host='0.0.0.0', port=5000):
    """运行 Flask API 服务"""
    logging.info(f"🌐 API服务启动在 http://{host}:{port}")
    logging.info(f"📍 可用端点:")
    logging.info(f"   - GET /api/servers/ips - 获取所有服务器IP")
    logging.info(f"   - GET /api/servers/status - 获取服务器详细状态")
    app.run(host=host, port=port, debug=False, use_reloader=False)


def main():
    global manager_instance
    
    # 基础配置
    API_KEY = os.getenv('HETZNER_API_KEY')
    THRESHOLD = float(os.getenv('TRAFFIC_THRESHOLD', '0.8'))
    INTERVAL = int(os.getenv('CHECK_INTERVAL', '1800'))
    
    # API 服务配置
    ENABLE_API = os.getenv('ENABLE_API', 'true').lower() == 'true'
    API_HOST = os.getenv('API_HOST', '0.0.0.0')
    API_PORT = int(os.getenv('API_PORT', '5000'))
    
    # 🆕 负载均衡器配置
    QB_LOADBALANCER_URL = os.getenv('QB_LOADBALANCER_URL')  # 例如: http://localhost:5000
    
    # SSH 密钥配置
    keys_raw = os.getenv('HETZNER_SSH_KEYS', '')
    ssh_keys = [int(k.strip()) for k in keys_raw.split(',') if k.strip().isdigit()]

    # 服务器类型优先级配置
    types_raw = os.getenv('SERVER_TYPES', '116,110,117')
    server_types = [int(t.strip()) for t in types_raw.split(',') if t.strip().isdigit()]

    # 服务器数量限制
    max_servers = int(os.getenv('MAX_SERVERS', '0'))
    
    # 初始快照ID配置
    initial_snapshot_id = os.getenv('INITIAL_SNAPSHOT_ID')
    if initial_snapshot_id:
        initial_snapshot_id = int(initial_snapshot_id)

    # Telegram 通知配置
    tg_token = os.getenv('TELEGRAM_BOT_TOKEN')
    tg_id = os.getenv('TELEGRAM_CHAT_ID')
    notifier = TelegramNotifier(tg_token, tg_id) if tg_token and tg_id else None

    # 时间窗口配置
    work_start = int(os.getenv('WORK_START_HOUR', '8'))
    work_end_hour = int(os.getenv('WORK_END_HOUR', '23'))
    work_end_minute = int(os.getenv('WORK_END_MINUTE', '30'))
    enable_time_window = os.getenv('ENABLE_TIME_WINDOW', 'false').lower() == 'true'
    
    time_window = TimeWindowManager(work_start, work_end_hour, work_end_minute) if enable_time_window else None

    # 下载器 API 配置
    downloader_url = os.getenv('DOWNLOADER_API_URL')
    downloader_cookies = os.getenv('DOWNLOADER_COOKIES')
    downloader_api = DownloaderAPI(downloader_url, downloader_cookies) if downloader_url and downloader_cookies else None

    if not API_KEY:
        print("❌ 错误: 环境变量中未找到 HETZNER_API_KEY")
        return

    manager_instance = HetznerServerManager(
        API_KEY, 
        THRESHOLD, 
        notifier, 
        downloader_api,
        time_window,
        ssh_keys, 
        server_types,
        max_servers,
        initial_snapshot_id,
        QB_LOADBALANCER_URL  # 🆕 传入负载均衡器URL
    )
    
    # 启动 API 服务（在单独线程中）
    if ENABLE_API:
        api_thread = Thread(target=run_flask_api, args=(API_HOST, API_PORT), daemon=True)
        api_thread.start()
        time.sleep(1)  # 等待API服务启动
    
    # 启动监控服务
    manager_instance.run_monitor(INTERVAL)


if __name__ == "__main__":
    main()
