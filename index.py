import logging
import os
import json
import smtplib
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
JSON_FOLDER_PATH = "./page/data"

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


# 通用重试装饰器
def create_retry_decorator(stop_attempts=RETRY_ATTEMPTS, wait_strategy=None):
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


request_retry = create_retry_decorator(
    wait_strategy=wait_chain(
        wait_fixed(15),
        wait_fixed(30),
        wait_exponential(multiplier=1, min=45, max=120)
    )
)


class EnergyMonitor:
    """电量监控器，负责获取电量信息"""

    def __init__(self):
        self.cas = CASClient(ACCOUNT, PASSWORD)
        self.get_energy_balance = create_retry_decorator()(self._get_energy_balance)

    def _get_energy_balance(self) -> Dict[str, float]:
        logger.info("尝试登录 ZZUPy 系统...")
        self.cas.login()
        logger.info("登录成功")

        lt_balance = 0.0
        ac_balance = 0.0

        try:
            ecard = ECardClient(self.cas)
            lt_balance = ecard.get_remaining_energy(LT_ROOM)
            ac_balance = ecard.get_remaining_energy(AC_ROOM)
        except Exception as e:
            logger.error("调用 ECardClient 获取电量失败：%s", e)
            try:
                self.cas.logout()
            except Exception:
                pass
            raise

        logger.info(f"照明剩余电量：{lt_balance} 度，空调剩余电量：{ac_balance} 度")
        self.cas.logout()
        logger.info("已登出 ZZUPy 系统")
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
            try:
                r = requests.post(url, data=payload, timeout=10)
                res_json = r.json()
                if res_json.get("code") == 0:
                    logger.info(f"Server 酱通知发送成功，使用的密钥：{key}")
                else:
                    logger.error(f"Server 酱通知发送失败：{res_json.get('message')}")
            except Exception as e:
                logger.error("Server酱通知发送异常：%s", e)

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
            logger.error(f"邮件通知发送失败：{e}")
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
        try:
            r = requests.post(url, data=payload, timeout=10)
            result = r.json()
            if not result.get("ok"):
                raise Exception(result.get("description", "未知错误"))
            logger.info("Telegram 通知发送成功")
        except Exception as e:
            logger.error("Telegram 通知发送失败：%s", e)
            raise

    @classmethod
    def notify_admin(cls, title: str, balances: Dict[str, float]) -> None:
        logger.info("准备发送通知...")

        is_low_energy = cls.is_low_energy(balances)
        email_content = cls.format_balance_report(balances["lt_Balance"], balances["ac_Balance"])

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
        cst_time = datetime.now(cst_tz)
        return cst_time.strftime(format_str)

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
                makedirs(dirpath, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=indent)
            logger.info(f"数据成功保存到文件：{file_path}")
        except Exception as e:
            logger.error(f"保存数据失败：{file_path}，错误信息：{e}")

    @classmethod
    def record_data(cls, data: Dict) -> Optional[List[Dict]]:
        file_path = f"{JSON_FOLDER_PATH}/{cls.get_cst_time_str('%Y-%m')}.json"
        existing_data = cls.load_data_from_json(file_path) or []
        existing_data.append(data)
        cls.dump_data_into_json(existing_data, file_path)
        return existing_data

    @classmethod
    def update_time_list(cls) -> List[str]:
        if not path.exists(JSON_FOLDER_PATH):
            raise FileNotFoundError(f"文件夹路径不存在：{JSON_FOLDER_PATH}")

        time_json_path = './page/data/time.json'
        if not path.exists(time_json_path):
            logger.warning("time.json 文件不存在，正在创建空文件...")
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
        time_file_list = cls.update_time_list()
        existing_data_length = len(existing_data) if existing_data else 0

        if existing_data_length < 30 and len(time_file_list) > 1:
            prev_month_data = cls.load_data_from_json(f"{JSON_FOLDER_PATH}/{time_file_list[1]}.json") or []
            records_to_retrieve = min(30 - existing_data_length, len(prev_month_data))
            existing_data = prev_month_data[-records_to_retrieve:] + (existing_data or [])

        cls.dump_data_into_json((existing_data or [])[-30:], f"{JSON_FOLDER_PATH}/last_30_records.json")
        logger.info("数据解析和更新完成")


def main():
    logger.info("启动宿舍电量监控程序...")

    required_env_vars = ["ACCOUNT", "PASSWORD", "lt_room", "ac_room"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(f"缺少必要的环境变量: {', '.join(missing_vars)}")
        return

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

    data = DataManager.record_data(latest_record)
    DataManager.parse_and_update_data(data)
    logger.info("程序运行结束")


if __name__ == "__main__":
    main()