import logging
import os
import json
import smtplib
import zipfile
import io
import sys
import threading
from datetime import datetime
from email.mime.text import MIMEText
from glob import glob
from os import makedirs, path
from typing import List, Dict, Optional, Union

import pytz
import requests
from tenacity import (
    retry, 
    stop_after_attempt, 
    wait_exponential,
    wait_chain,
    wait_fixed,
    retry_if_exception_type
)
from zzupy.app import CASClient, ECardClient

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 常量定义
THRESHOLD = 10.0
EXCELLENT_THRESHOLD = 100.0

# 使用绝对路径避免问题
BASE_DIR = path.dirname(path.abspath(__file__))
JSON_FOLDER_PATH = path.join(BASE_DIR, "page", "data")
TOKEN_ZIP_PATH = path.join(JSON_FOLDER_PATH, "tokens.zip")

# 重试配置常量
RETRY_ATTEMPTS = 5
RETRY_MULTIPLIER = 1
INITIAL_WAIT = 15
MAX_WAIT = 120

# 环境变量
ACCOUNT = os.getenv("ACCOUNT")
PASSWORD = os.getenv("PASSWORD")
LT_ROOM = os.getenv("lt_room")
AC_ROOM = os.getenv("ac_room")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SERVERCHAN_KEYS = os.getenv("SERVERCHAN_KEYS")
EMAIL = os.getenv("EMAIL")
SMTP_CODE = os.getenv("SMTP_CODE")
SMTP_SERVER = os.getenv("SMTP_SERVER")


# 确保目录存在
def ensure_directory_exists(dir_path: str) -> None:
    """确保目录存在，如果不存在则创建"""
    if not path.exists(dir_path):
        try:
            makedirs(dir_path, exist_ok=True)
            logger.info(f"创建目录: {dir_path}")
        except Exception as e:
            logger.error(f"创建目录失败 {dir_path}: {e}")
            raise


# 通用重试装饰器
def create_retry_decorator(stop_attempts=RETRY_ATTEMPTS, wait_strategy=None):
    """创建统一的重试装饰器"""
    if wait_strategy is None:
        wait_strategy = wait_exponential(
            multiplier=RETRY_MULTIPLIER, 
            min=INITIAL_WAIT, 
            max=MAX_WAIT
        )
    
    return retry(
        stop=stop_after_attempt(stop_attempts),
        wait=wait_strategy,
        retry=retry_if_exception_type(Exception),
        reraise=True
    )

# 通用的请求重试装饰器
request_retry = create_retry_decorator(
    wait_strategy=wait_chain(
        wait_fixed(15),  # 第一次等待15s
        wait_fixed(30),  # 第二次等待30s
        wait_exponential(multiplier=1, min=45, max=120)  # 后续按指数退避
    )
)


class TokenManager:
    """Token管理器，负责token的保存和读取"""
    
    @staticmethod
    def save_tokens(user_token: str, refresh_token: str) -> None:
        """保存token到加密的zip文件"""
        temp_json_path = None
        try:
            # 确保目录存在
            ensure_directory_exists(JSON_FOLDER_PATH)
            
            token_data = {
                "user_token": user_token,
                "refresh_token": refresh_token,
                "saved_at": DataManager.get_cst_time_str("%Y-%m-%d %H:%M:%S")
            }
            
            # 创建临时json文件
            temp_json_path = path.join(JSON_FOLDER_PATH, "tokens_temp.json")
            with open(temp_json_path, 'w', encoding='utf-8') as f:
                json.dump(token_data, f, ensure_ascii=False, indent=2)
            
            # 使用pyminizip加密压缩
            try:
                import pyminizip
                pyminizip.compress(
                    temp_json_path,  # 输入文件
                    "",  # 不添加前缀
                    TOKEN_ZIP_PATH,  # 输出文件
                    PASSWORD,  # 密码
                    5  # 压缩级别 (0-9)
                )
            except ImportError:
                logger.warning("pyminizip未安装，使用普通zip文件存储（无加密）")
                # 备用方案：使用普通zip文件
                with zipfile.ZipFile(TOKEN_ZIP_PATH, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.write(temp_json_path, "tokens_temp.json")
            
            logger.info(f"Token已保存到文件: {TOKEN_ZIP_PATH}")
        except Exception as e:
            logger.error(f"保存token失败: {e}")
            raise
        finally:
            # 清理临时文件
            if temp_json_path and path.exists(temp_json_path):
                try:
                    os.remove(temp_json_path)
                except Exception as e:
                    logger.warning(f"删除临时文件失败: {e}")

    @staticmethod
    def load_tokens() -> Optional[Dict[str, str]]:
        """从加密的zip文件加载token"""
        try:
            if not path.exists(TOKEN_ZIP_PATH):
                logger.info("Token文件不存在，将使用账号密码登录")
                return None
            
            # 创建临时目录用于解压
            import tempfile
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    # 尝试使用pyminizip解压
                    import pyminizip
                    pyminizip.uncompress(
                        TOKEN_ZIP_PATH,
                        PASSWORD,
                        temp_dir,
                        0  # 不保留文件结构
                    )
                except ImportError:
                    # 备用方案：使用普通zip文件
                    logger.warning("pyminizip未安装，尝试使用普通zip文件读取")
                    with zipfile.ZipFile(TOKEN_ZIP_PATH, 'r') as zf:
                        zf.extractall(temp_dir)
                
                # 读取解压后的文件
                token_file_path = path.join(temp_dir, "tokens_temp.json")
                if not path.exists(token_file_path):
                    logger.warning("Token文件格式不正确")
                    return None
                    
                with open(token_file_path, 'r', encoding='utf-8') as f:
                    token_data = json.load(f)
            
            logger.info(f"从文件加载token成功，保存时间: {token_data.get('saved_at', '未知')}")
            return token_data
            
        except Exception as e:
            logger.warning(f"读取token文件失败，将使用账号密码登录: {e}")
            return None

    @staticmethod
    def delete_tokens() -> None:
        """删除token文件"""
        try:
            if path.exists(TOKEN_ZIP_PATH):
                os.remove(TOKEN_ZIP_PATH)
                logger.info("Token文件已删除")
        except Exception as e:
            logger.error(f"删除token文件失败: {e}")


class EnergyMonitor:
    """电量监控器，负责获取电量信息"""
    
    def __init__(self):
        self.cas_client = CASClient(ACCOUNT, PASSWORD)
        self.get_energy_balance = create_retry_decorator()(self._get_energy_balance)

    def _initialize_cas_client(self) -> bool:
        """初始化CAS客户端，尝试使用token登录"""
        token_data = TokenManager.load_tokens()
        
        if token_data and token_data.get('user_token') and token_data.get('refresh_token'):
            try:
                logger.info("尝试使用保存的token登录...")
                self.cas_client.set_token(token_data['user_token'], token_data['refresh_token'])
                self.cas_client.login()
                if self.cas_client.logged_in:
                    logger.info("使用保存的token登录成功")
                    return True
                else:
                    logger.warning("保存的token已失效，将使用账号密码重新登录")
                    # token失效时删除文件
                    TokenManager.delete_tokens()
            except Exception as e:
                logger.warning(f"使用token登录失败: {e}，将使用账号密码登录")
                # token失效时删除文件
                TokenManager.delete_tokens()
        
        logger.info("使用账号密码进行CAS认证...")
        self.cas_client.login()
        if self.cas_client.logged_in:
            logger.info("CAS认证成功")
            try:
                TokenManager.save_tokens(self.cas_client.user_token, self.cas_client.refresh_token)
            except Exception as e:
                logger.error(f"保存token失败: {e}")
            return True
        else:
            logger.error("CAS认证失败")
            return False

    def _get_energy_balance(self) -> Dict[str, float]:
        """使用新的 zzupy 库获取电量余额"""
        if not self._initialize_cas_client():
            raise Exception("CAS认证失败，无法获取电量信息")
        
        logger.info("创建一卡通客户端并登录...")
        with ECardClient(self.cas_client) as ecard:
            ecard.login()
            logger.info("一卡通系统登录成功")
            
            logger.info("获取照明和空调电量余额...")
            lt_balance = ecard.get_remaining_energy(room=LT_ROOM)
            ac_balance = ecard.get_remaining_energy(room=AC_ROOM)
            
            logger.info(f"照明剩余电量：{lt_balance} 度，空调剩余电量：{ac_balance} 度")
            return {"lt_Balance": lt_balance, "ac_Balance": ac_balance}


class NotificationManager:
    """通知管理器，负责发送各种通知"""
    
    @staticmethod
    def format_balance_report(lt_balance: float, ac_balance: float, escape_dot: bool = False) -> str:
        def get_status(balance: float) -> str:
            if balance > EXCELLENT_THRESHOLD:
                return "充足"
            elif balance > THRESHOLD:
                return "还行"
            else:
                return "⚠️警告"

        lt_status = get_status(lt_balance)
        ac_status = get_status(ac_balance)
        lt_balance_str = str(lt_balance).replace(".", "\\.") if escape_dot else str(lt_balance)
        ac_balance_str = str(ac_balance).replace(".", "\\.") if escape_dot else str(ac_balance)

        return (
            f"💡 照明剩余电量：{lt_balance_str} 度（{lt_status}）\n"
            f"❄️ 空调剩余电量：{ac_balance_str} 度（{ac_status}）\n\n"
        )

    @staticmethod
    def is_low_energy(balances: Dict[str, float]) -> bool:
        return balances['lt_Balance'] <= THRESHOLD or balances['ac_Balance'] <= THRESHOLD

    @staticmethod
    @request_retry
    def send_serverchan_notification(title: str, content: str) -> None:
        if not SERVERCHAN_KEYS:
            logger.info("未配置 SERVERCHAN_KEYS，跳过 Server 酱通知")
            return
            
        logger.info("通过 Server 酱发送通知...")
        for key in SERVERCHAN_KEYS.split(','):
            key = key.strip()
            if not key:
                continue
                
            url = f"https://sctapi.ftqq.com/{key}.send"
            payload = {"title": title, "desp": content}
            response = requests.post(url, data=payload, timeout=10)
            try:
                result = response.json()
            except ValueError:
                logger.error("Server酱返回非 JSON，返回文本：%s", response.text)
                continue

            if result.get("code") == 0:
                logger.info(f"Server 酱通知发送成功，使用的密钥：{key}")
            else:
                logger.error(f"Server 酱通知发送失败，错误信息：{result.get('message')}")

    @staticmethod
    @create_retry_decorator()
    def send_email_notification(title: str, content: str) -> None:
        if not all([EMAIL, SMTP_CODE, SMTP_SERVER]):
            logger.info("邮件配置不完整，跳过邮件通知")
            return
            
        logger.info("通过邮件发送通知...")
        msg = MIMEText(content, 'plain', 'utf-8')
        msg['Subject'] = title
        msg['From'] = EMAIL
        msg['To'] = EMAIL

        try:
            client = smtplib.SMTP_SSL(SMTP_SERVER, smtplib.SMTP_SSL_PORT)
            client.login(EMAIL, SMTP_CODE)
            client.sendmail(EMAIL, EMAIL, msg.as_string())
            client.quit()
            logger.info("邮件发送成功")
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            raise

    @staticmethod
    @request_retry
    def send_telegram_notification(title: str, content: str) -> None:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.info("未配置 Telegram 参数，跳过 Telegram 通知")
            return
            
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"*{title}*\n\n{content}",
            "parse_mode": "MarkdownV2"
        }
        response = requests.post(url, data=payload, timeout=10)
        result = response.json()

        if not result.get("ok"):
            raise requests.exceptions.RequestException(result.get("description"))
        logger.info("Telegram 通知发送成功")

    @classmethod
    def notify_admin(cls, title: str, balances: Dict[str, float]) -> None:
        logger.info("准备发送通知...")
        is_low_energy = cls.is_low_energy(balances)
        email_content = cls.format_balance_report(balances["lt_Balance"], balances["ac_Balance"], escape_dot=False)
        
        if is_low_energy:
            email_content += "⚠️ 电量不足，请尽快充电！"
            cls.send_serverchan_notification(title, email_content)
            cls.send_email_notification(title, email_content)
        else:
            logger.info("电量充足，跳过 Server 酱和邮件通知")

        telegram_content = cls.format_balance_report(balances["lt_Balance"], balances["ac_Balance"], escape_dot=True)
        telegram_content += "⚠️ 电量不足，请尽快充电！" if is_low_energy else "当前电量充足，请保持关注。"
        cls.send_telegram_notification(title, telegram_content)


class DataManager:
    """数据管理器"""
    
    @staticmethod
    def get_cst_time_str(format_str: str) -> str:
        cst_tz = pytz.timezone('Asia/Shanghai')
        return datetime.now(cst_tz).strftime(format_str)

    @staticmethod
    def load_data_from_json(file_path: str) -> Optional[List[Dict]]:
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"加载JSON文件失败 {file_path}: {e}")
            return None

    @staticmethod
    def dump_data_into_json(data: Union[List, Dict], file_path: str, indent: int = 4) -> None:
        try:
            dirpath = path.dirname(file_path)
            if dirpath and not path.exists(dirpath):
                ensure_directory_exists(dirpath)
            with open(file_path, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=indent)
            logger.info(f"数据成功保存到文件：{file_path}")
        except Exception as e:
            logger.error(f"保存数据失败：{e}")

    @classmethod
    def record_data(cls, data: Dict) -> Optional[List[Dict]]:
        file_path = f"{JSON_FOLDER_PATH}/{cls.get_cst_time_str('%Y-%m')}.json"
        existing_data = cls.load_data_from_json(file_path) or []
        existing_data.append(data)
        cls.dump_data_into_json(existing_data, file_path)
        return existing_data

    @classmethod
    def update_time_list(cls) -> List[str]:
        # 确保数据目录存在
        ensure_directory_exists(JSON_FOLDER_PATH)

        time_json_path = path.join(BASE_DIR, 'page', 'data', 'time.json')
        
        # 确保time.json的目录存在
        ensure_directory_exists(path.dirname(time_json_path))

        if not path.exists(time_json_path):
            cls.dump_data_into_json([], time_json_path)

        json_files = [
            path.splitext(path.basename(it))[0] 
            for it in glob(path.join(JSON_FOLDER_PATH, "????-??.json"))
        ]
        json_files = sorted(json_files, key=lambda x: datetime.strptime(x, '%Y-%m'), reverse=True)

        cls.dump_data_into_json(json_files, time_json_path)
        logger.info("时间列表更新成功")
        return json_files

    @classmethod
    def parse_and_update_data(cls, existing_data: Optional[List[Dict]]) -> None:
        try:
            time_file_list = cls.update_time_list()
            existing_data_length = len(existing_data) if existing_data else 0
            if existing_data_length < 30 and len(time_file_list) > 1:
                prev_month_data = cls.load_data_from_json(f"{JSON_FOLDER_PATH}/{time_file_list[1]}.json") or []
                records_to_retrieve = min(30 - existing_data_length, len(prev_month_data))
                existing_data = prev_month_data[-records_to_retrieve:] + (existing_data or [])
            
            last_30_records_path = path.join(JSON_FOLDER_PATH, "last_30_records.json")
            cls.dump_data_into_json((existing_data or [])[-30:], last_30_records_path)
            logger.info("数据解析和更新完成")
        except Exception as e:
            logger.error(f"数据解析和更新失败: {e}")


def main():
    logger.info("启动宿舍电量监控程序...")

    required_env_vars = ["ACCOUNT", "PASSWORD", "lt_room", "ac_room"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"缺少必要的环境变量: {', '.join(missing_vars)}")
        return

    # 确保数据目录存在
    ensure_directory_exists(JSON_FOLDER_PATH)

    monitor = EnergyMonitor()
    try:
        balances = monitor.get_energy_balance()
    except Exception as e:
        logger.error("获取电量失败：%s", e)
        return

    title = "⚠️宿舍电量预警⚠️" if NotificationManager.is_low_energy(balances) else "🏠宿舍电量通报🏠"
    NotificationManager.notify_admin(title, balances)

    latest_record = {
        "time": DataManager.get_cst_time_str("%m-%d %H:%M:%S"),
        "lt_Balance": balances["lt_Balance"],
        "ac_Balance": balances["ac_Balance"]
    }

    try:
        data = DataManager.record_data(latest_record)
        DataManager.parse_and_update_data(data)
        logger.info("程序运行结束")
    except Exception as e:
        logger.error(f"数据记录失败: {e}")


if __name__ == "__main__":
    main()

    # 打印存活线程，辅助调试
    for t in threading.enumerate():
        print(f"存活线程: {t.name}, daemon={t.daemon}")

    # 优雅退出
    import time
    logging.shutdown()
    time.sleep(0.5)
    os._exit(0)  # 确保彻底退出
