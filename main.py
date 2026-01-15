import json
import re
import os
import random
from typing import Dict, Any, List
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api import logger, AstrBotConfig


CURRENCY_UNIT = "元"


class ThankLetterManager:
    """
    表扬信管理系统
    - 记录每日发送限制（每账号每天一封）
    - 记录历史表扬信排行
    - 记录今日表扬奖金
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._init_path()
        self.data = self._load_data()

    def _init_path(self):
        """初始化数据目录"""
        os.makedirs(self.data_dir, exist_ok=True)

    def _load_data(self) -> Dict[str, Any]:
        """加载表扬信数据"""
        path = os.path.join(self.data_dir, "thank_letters.json")
        if not os.path.exists(path):
            return {
                "daily_senders": {},  # {"2024-01-01": ["sender_id1", "sender_id2"]}
                "ranking": {},  # {"sender_id": count}
                "today_bonus": 0,  # 今日表扬奖金
                "today_date": "",  # 今日日期
                "total_bonus": 0  # 累计表扬奖金
            }
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 确保所有字段存在
                if "daily_senders" not in data:
                    data["daily_senders"] = {}
                if "ranking" not in data:
                    data["ranking"] = {}
                if "today_bonus" not in data:
                    data["today_bonus"] = 0
                if "today_date" not in data:
                    data["today_date"] = ""
                if "total_bonus" not in data:
                    data["total_bonus"] = 0
                return data
        except (json.JSONDecodeError, TypeError):
            return {
                "daily_senders": {},
                "ranking": {},
                "today_bonus": 0,
                "today_date": "",
                "total_bonus": 0
            }

    def _save_data(self):
        """保存表扬信数据"""
        path = os.path.join(self.data_dir, "thank_letters.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _check_and_reset_daily(self):
        """检查并重置每日数据（24点重置）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.data["today_date"] != today:
            self.data["today_date"] = today
            self.data["today_bonus"] = 0
            # 清理过期的每日记录（保留最近7天）
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            self.data["daily_senders"] = {
                k: v for k, v in self.data["daily_senders"].items() if k >= cutoff
            }
            self._save_data()

    def can_send_today(self, sender_id: str) -> bool:
        """检查该用户今天是否还能发送表扬信"""
        self._check_and_reset_daily()
        today = datetime.now().strftime("%Y-%m-%d")
        today_senders = self.data["daily_senders"].get(today, [])
        return sender_id not in today_senders

    def record_thank_letter(self, sender_id: str, sender_name: str, amount: int) -> bool:
        """
        记录一封表扬信
        :return: 是否成功
        """
        self._check_and_reset_daily()
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 检查今日是否已发送
        if not self.can_send_today(sender_id):
            return False
        
        # 记录今日发送者
        if today not in self.data["daily_senders"]:
            self.data["daily_senders"][today] = []
        self.data["daily_senders"][today].append(sender_id)
        
        # 更新排行榜（使用sender_id作为key，同时存储sender_name）
        ranking_key = f"{sender_id}|{sender_name}"
        # 先查找是否有旧的记录（可能名字变了）
        old_key = None
        for key in self.data["ranking"]:
            if key.startswith(f"{sender_id}|"):
                old_key = key
                break
        if old_key and old_key != ranking_key:
            # 名字变了，迁移数据
            self.data["ranking"][ranking_key] = self.data["ranking"].pop(old_key) + 1
        else:
            self.data["ranking"][ranking_key] = self.data["ranking"].get(ranking_key, 0) + 1
        
        # 更新今日奖金
        self.data["today_bonus"] += amount
        self.data["total_bonus"] += amount
        
        self._save_data()
        return True

    def get_today_bonus(self) -> int:
        """获取今日表扬奖金"""
        self._check_and_reset_daily()
        return self.data.get("today_bonus", 0)

    def get_total_bonus(self) -> int:
        """获取累计表扬奖金"""
        return self.data.get("total_bonus", 0)

    def get_ranking(self, top_n: int = 10) -> List[tuple]:
        """获取表扬信排行榜"""
        ranking = self.data.get("ranking", {})
        # 排序并返回前N名
        sorted_ranking = sorted(ranking.items(), key=lambda x: x[1], reverse=True)
        return sorted_ranking[:top_n]


class BackpackManager:
    """
    小背包管理系统
    - 共享背包：贝塔自己的物品存储（10个格子，只能放自己的东西）
    - 专属格子：每个用户有3个专属格子（跨窗口，存放收到的礼物）
    - 数据结构: 
      - shared_items: [{"name": str, "description": str, "time": str}]  # 共享背包
      - user_slots: {"user_id": [{"name": str, "description": str, "from": str, "time": str}]}  # 用户专属格子
    """

    def __init__(self, data_dir: str, max_shared_slots: int = 10, max_user_slots: int = 3):
        self.data_dir = data_dir
        self.max_shared_slots = max_shared_slots
        self.max_user_slots = max_user_slots
        self._init_path()
        self.data = self._load_data()

    def _init_path(self):
        """初始化数据目录"""
        os.makedirs(self.data_dir, exist_ok=True)

    def _load_data(self) -> Dict[str, Any]:
        """加载背包数据"""
        path = os.path.join(self.data_dir, "backpack.json")
        if not os.path.exists(path):
            return {"shared_items": [], "user_slots": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 兼容旧版本数据结构
                if "items" in data and "shared_items" not in data:
                    # 迁移旧数据
                    data["shared_items"] = data.pop("items")
                if "shared_items" not in data:
                    data["shared_items"] = []
                if "user_slots" not in data:
                    data["user_slots"] = {}
                return data
        except (json.JSONDecodeError, TypeError):
            return {"shared_items": [], "user_slots": {}}

    def _save_data(self):
        """保存背包数据"""
        path = os.path.join(self.data_dir, "backpack.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ========== 共享背包操作 ==========
    
    def get_shared_items(self) -> List[Dict[str, Any]]:
        """获取共享背包所有物品"""
        return self.data.get("shared_items", [])

    def get_shared_item_count(self) -> int:
        """获取共享背包物品数量"""
        return len(self.data.get("shared_items", []))

    def is_shared_full(self) -> bool:
        """检查共享背包是否已满"""
        return self.get_shared_item_count() >= self.max_shared_slots

    def add_shared_item(self, name: str, description: str) -> bool:
        """
        添加物品到共享背包（贝塔自己的东西）
        :return: 是否成功
        """
        if self.is_shared_full():
            return False
        
        item = {
            "name": name,
            "description": description,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.data["shared_items"].append(item)
        self._save_data()
        return True

    def use_shared_item(self, name: str) -> bool:
        """
        使用（移除）共享背包物品
        :return: 是否成功
        """
        items = self.data.get("shared_items", [])
        for i, item in enumerate(items):
            if item["name"] == name:
                items.pop(i)
                self._save_data()
                return True
        return False

    def clear_shared_items(self):
        """清空共享背包"""
        self.data["shared_items"] = []
        self._save_data()

    # ========== 用户专属格子操作 ==========
    
    def get_user_items(self, user_id: str) -> List[Dict[str, Any]]:
        """获取指定用户的专属格子物品"""
        return self.data.get("user_slots", {}).get(user_id, [])

    def get_user_item_count(self, user_id: str) -> int:
        """获取指定用户的专属格子物品数量"""
        return len(self.get_user_items(user_id))

    def is_user_slots_full(self, user_id: str) -> bool:
        """检查指定用户的专属格子是否已满"""
        return self.get_user_item_count(user_id) >= self.max_user_slots

    def add_user_gift(self, user_id: str, name: str, description: str, from_who: str) -> bool:
        """
        添加礼物到用户专属格子
        :param user_id: 用户ID
        :param name: 物品名
        :param description: 描述
        :param from_who: 送礼人
        :return: 是否成功
        """
        if self.is_user_slots_full(user_id):
            return False
        
        if "user_slots" not in self.data:
            self.data["user_slots"] = {}
        if user_id not in self.data["user_slots"]:
            self.data["user_slots"][user_id] = []
        
        item = {
            "name": name,
            "description": description,
            "from": from_who,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.data["user_slots"][user_id].append(item)
        self._save_data()
        return True

    def use_user_item(self, user_id: str, name: str) -> bool:
        """
        使用（移除）用户专属格子物品
        :return: 是否成功
        """
        items = self.data.get("user_slots", {}).get(user_id, [])
        for i, item in enumerate(items):
            if item["name"] == name:
                items.pop(i)
                self._save_data()
                return True
        return False

    def clear_user_items(self, user_id: str):
        """清空指定用户的专属格子"""
        if user_id in self.data.get("user_slots", {}):
            self.data["user_slots"][user_id] = []
            self._save_data()

    def get_all_user_slots(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取所有用户的专属格子数据"""
        return self.data.get("user_slots", {})

    # ========== 格式化方法 ==========
    
    def format_shared_items_for_prompt(self) -> str:
        """格式化共享背包物品列表用于提示词"""
        items = self.get_shared_items()
        if not items:
            return "空空如也"
        return "、".join([f"{item['name']}({item['description']})" for item in items])

    def format_user_items_for_prompt(self, user_id: str) -> str:
        """格式化用户专属格子物品列表用于提示词"""
        items = self.get_user_items(user_id)
        if not items:
            return "空空如也"
        return "、".join([f"{item['name']}(来自{item['from']}: {item['description']})" for item in items])

    # ========== 兼容旧版本的方法别名 ==========
    
    def get_items(self) -> List[Dict[str, Any]]:
        """获取所有物品（兼容旧版本，返回共享背包）"""
        return self.get_shared_items()

    def get_item_count(self) -> int:
        """获取物品数量（兼容旧版本，返回共享背包）"""
        return self.get_shared_item_count()

    def is_full(self) -> bool:
        """检查背包是否已满（兼容旧版本，检查共享背包）"""
        return self.is_shared_full()

    def add_item(self, name: str, description: str) -> bool:
        """添加物品（兼容旧版本，添加到共享背包）"""
        return self.add_shared_item(name, description)

    def use_item(self, name: str) -> bool:
        """使用物品（兼容旧版本，从共享背包移除）"""
        return self.use_shared_item(name)

    def clear_items(self):
        """清空背包（兼容旧版本，清空共享背包）"""
        self.clear_shared_items()

    def format_items_for_prompt(self) -> str:
        """格式化物品列表（兼容旧版本，返回共享背包）"""
        return self.format_shared_items_for_prompt()

    @property
    def max_slots(self) -> int:
        """兼容旧版本的属性"""
        return self.max_shared_slots


class PocketMoneyManager:
    """
    小金库管理系统
    - 全局余额管理（不区分会话）
    - 入账/出账记录
    - 数据结构: {"balance": float, "records": [{"type": "income/expense", "amount": float, "reason": str, "time": str, "operator": str}]}
    """

    def __init__(self, data_dir: str, initial_balance: float = 0, max_records: int = 100):
        self.data_dir = data_dir
        self.initial_balance = initial_balance
        self.max_records = max_records
        self._init_path()
        self.data = self._load_data()

    def _init_path(self):
        """初始化数据目录"""
        os.makedirs(self.data_dir, exist_ok=True)

    def _load_data(self) -> Dict[str, Any]:
        """加载金库数据"""
        path = os.path.join(self.data_dir, "pocket_money.json")
        if not os.path.exists(path):
            return {"balance": self.initial_balance, "records": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "balance" not in data:
                    data["balance"] = self.initial_balance
                if "records" not in data:
                    data["records"] = []
                return data
        except (json.JSONDecodeError, TypeError):
            return {"balance": self.initial_balance, "records": []}

    def _save_data(self):
        """保存金库数据"""
        path = os.path.join(self.data_dir, "pocket_money.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_balance(self) -> float:
        """获取当前余额"""
        return self.data.get("balance", 0)

    def get_recent_records(self, count: int = 5) -> List[Dict[str, Any]]:
        """获取最近的记录"""
        records = self.data.get("records", [])
        return records[-count:] if records else []

    def get_recent_income_records(self, count: int = 2) -> List[Dict[str, Any]]:
        """获取最近的入账记录"""
        records = self.data.get("records", [])
        income_records = [r for r in records if r["type"] == "income"]
        return income_records[-count:] if income_records else []

    def get_recent_expense_records(self, count: int = 5) -> List[Dict[str, Any]]:
        """获取最近的出账记录"""
        records = self.data.get("records", [])
        expense_records = [r for r in records if r["type"] == "expense"]
        return expense_records[-count:] if expense_records else []

    def get_all_records(self) -> List[Dict[str, Any]]:
        """获取所有记录"""
        return self.data.get("records", [])

    def add_income(self, amount: float, reason: str, operator: str) -> bool:
        """
        入账（只能由管理员操作）
        :param amount: 金额（正数）
        :param reason: 原因
        :param operator: 操作者
        :return: 是否成功
        """
        if amount <= 0:
            return False
        
        self.data["balance"] = self.get_balance() + amount
        record = {
            "type": "income",
            "amount": amount,
            "reason": reason,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator": operator
        }
        self.data["records"].append(record)
        
        # 限制记录数量
        if len(self.data["records"]) > self.max_records:
            self.data["records"] = self.data["records"][-self.max_records:]
        
        self._save_data()
        return True

    def add_expense(self, amount: float, reason: str, operator: str = "贝塔") -> bool:
        """
        出账（AI自主或管理员操作）
        :param amount: 金额（正数）
        :param reason: 原因
        :param operator: 操作者
        :return: 是否成功
        """
        if amount <= 0:
            return False
        if amount > self.get_balance():
            return False
        
        self.data["balance"] = self.get_balance() - amount
        record = {
            "type": "expense",
            "amount": amount,
            "reason": reason,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator": operator
        }
        self.data["records"].append(record)
        
        # 限制记录数量
        if len(self.data["records"]) > self.max_records:
            self.data["records"] = self.data["records"][-self.max_records:]
        
        self._save_data()
        return True

    def set_balance(self, amount: float, reason: str, operator: str) -> bool:
        """
        直接设置余额（管理员操作）
        """
        old_balance = self.get_balance()
        self.data["balance"] = amount
        
        diff = amount - old_balance
        record_type = "income" if diff >= 0 else "expense"
        record = {
            "type": record_type,
            "amount": abs(diff),
            "reason": f"[余额调整] {reason}",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator": operator
        }
        self.data["records"].append(record)
        
        if len(self.data["records"]) > self.max_records:
            self.data["records"] = self.data["records"][-self.max_records:]
        
        self._save_data()
        return True


@register("astrbot_plugin_pocketmoney", "柯尔", "贝塔的小金库系统，管理余额和收支记录", "1.0.0")
class PocketMoneyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 使用插件数据目录
        self.data_dir = os.path.join("data", "PocketMoney")
        initial_balance = self.config.get("initial_balance", 0)
        max_records = self.config.get("max_records", 100)
        
        self.manager = PocketMoneyManager(self.data_dir, initial_balance, max_records)
        
        # 表扬信管理器
        self.thank_manager = ThankLetterManager(self.data_dir)
        
        # 小背包管理器
        max_shared_slots = self.config.get("max_shared_slots", 10)
        max_user_slots = self.config.get("max_user_slots", 3)
        self.backpack_manager = BackpackManager(self.data_dir, max_shared_slots, max_user_slots)

        # 匹配出账标记的正则表达式
        self.spend_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:Spend|花费|支出))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.amount_pattern = re.compile(r"(?:Spend|花费|支出)\s*[:：]\s*(\d+(?:\.\d+)?)")
        # 标准格式: [Spend: 1, Reason: 原因]
        self.reason_pattern = re.compile(r"(?:Reason|原因|用途)\s*[:：]\s*(.+?)(?=\s*[,，\]]|\])")
        # 省略标识符格式: [Spend: 1, 原因内容] - 匹配金额后逗号后的内容
        self.reason_fallback_pattern = re.compile(
            r"(?:Spend|花费|支出)\s*[:：]\s*\d+(?:\.\d+)?\s*[,，]\s*(.+?)(?=\s*\])"
        )
        
        # 匹配背包入库标记: [Store: 物品名, Desc: 描述]
        self.store_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:Store|入库|收纳))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.store_name_pattern = re.compile(r"(?:Store|入库|收纳)\s*[:：]\s*(.+?)(?=\s*[,，])")
        self.store_desc_pattern = re.compile(r"(?:Desc|描述|说明)\s*[:：]\s*(.+?)(?=\s*\])")
        
        # 匹配背包使用标记: [Use: 物品名]
        self.use_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:Use|使用|用掉))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.use_name_pattern = re.compile(r"(?:Use|使用|用掉)\s*[:：]\s*(.+?)(?=\s*\])")
        
        # 匹配礼物入库标记: [Gift: 物品名, From: 送礼人, Desc: 描述]
        self.gift_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:Gift|礼物|收礼))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.gift_name_pattern = re.compile(r"(?:Gift|礼物|收礼)\s*[:：]\s*(.+?)(?=\s*[,\uff0c])")
        self.gift_from_pattern = re.compile(r"(?:From|来自|送礼人)\s*[:：]\s*(.+?)(?=\s*[,\uff0c])")
        self.gift_desc_pattern = re.compile(r"(?:Desc|描述|说明)\s*[:：]\s*(.+?)(?=\s*\])")
        
        # 匹配使用专属格子物品标记: [UseGift: 物品名]
        self.use_gift_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:UseGift|使用礼物|用礼物))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.use_gift_name_pattern = re.compile(r"(?:UseGift|使用礼物|用礼物)\s*[:：]\s*(.+?)(?=\s*\])")
        
        # 匹配退款标记: [Refund: 金额, Reason: 原因]
        self.refund_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:Refund|退款|退钱))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.refund_amount_pattern = re.compile(r"(?:Refund|退款|退钱)\s*[:：]\s*(\d+(?:\.\d+)?)")
        self.refund_reason_pattern = re.compile(r"(?:Reason|原因|理由)\s*[:：]\s*(.+?)(?=\s*[,，\]]|\])")
        
        # 防重复扣费：记录已处理的消息ID
        self.processed_message_ids = set()

    def _format_records(self, records: List[Dict[str, Any]], show_type: bool = True) -> str:
        """格式化记录为字符串"""
        if not records:
            return "暂无"
        
        lines = []
        for r in records:
            if show_type:
                type_str = "+" if r["type"] == "income" else "-"
                lines.append(f"{r['time']}: {type_str}{r['amount']}{CURRENCY_UNIT} ({r['reason']})")
            else:
                lines.append(f"{r['time']}: {r['amount']}{CURRENCY_UNIT} ({r['reason']})")
        return "; ".join(lines)

    def _get_weekday_info(self) -> tuple:
        """获取星期信息，返回 (发薪日周几, 今天周几, 距离天数)"""
        allowance_day = self.config.get("allowance_day", 1)  # 1=周一, 7=周日
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        
        today = datetime.now()
        current_weekday = today.weekday()  # 0=周一, 6=周日
        
        # 配置是1-7，转换为0-6
        allowance_weekday_idx = (allowance_day - 1) % 7
        
        # 计算距离下一个发薪日的天数
        days_until = (allowance_weekday_idx - current_weekday) % 7
        if days_until == 0:
            days_until = 0  # 今天就是发薪日
        
        return (
            weekday_names[allowance_weekday_idx],
            weekday_names[current_weekday],
            days_until
        )

    @filter.on_llm_request()
    async def add_context_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """向LLM注入小金库状态"""
        balance = self.manager.get_balance()
        
        # 分别获取入账和出账记录
        income_count = self.config.get("income_record_count", 2)
        expense_count = self.config.get("expense_record_count", 5)
        income_records = self.manager.get_recent_income_records(income_count)
        expense_records = self.manager.get_recent_expense_records(expense_count)
        
        income_str = self._format_records(income_records, show_type=False)
        expense_str = self._format_records(expense_records, show_type=False)
        
        # 获取星期信息
        allowance_weekday, today_weekday, days_until = self._get_weekday_info()
        
        # 获取今日表扬奖金
        today_thank_bonus = self.thank_manager.get_today_bonus()
        
        # 获取当前用户ID
        current_user_id = event.get_sender_id()
        current_user_name = event.get_sender_name() or current_user_id
        
        # 获取共享背包信息
        shared_items = self.backpack_manager.format_shared_items_for_prompt()
        shared_slots = f"{self.backpack_manager.get_shared_item_count()}/{self.backpack_manager.max_shared_slots}"
        
        # 获取当前用户的专属格子信息
        user_items = self.backpack_manager.format_user_items_for_prompt(current_user_id)
        user_slots = f"{self.backpack_manager.get_user_item_count(current_user_id)}/{self.backpack_manager.max_user_slots}"

        # 构建小金库系统提示词
        pocketmoney_template = self.config.get("pocketmoney_prompt", 
            "<小金库系统>\n"
            "【功能介绍】这是你的小金库，由奥卢斯大人管理，全窗口通用的零花钱系统。\n"
            "【当前状态】\n"
            "- 余额：{balance}{unit}\n"
            "- 发零花钱日：{allowance_weekday}（今天{today_weekday}，还有{days_until}天）\n"
            "- 最近入账：{income_records}\n"
            "- 最近出账：{expense_records}\n"
            "- 本日表扬奖金：{today_thank_bonus}{unit}\n"
            "【花钱准则】\n"
            "1. 每笔花费控制在5元左右，特殊情况最多10元\n"
            "2. 花钱前要先告诉对方花多少、花在什么上，获得确认后再执行\n"
            "3. 可以自由拒绝别人的花钱要求\n"
            "4. 不能收别人的钱，入账只能由奥卢斯大人操作\n"
            "5. 绝对不允许找别人要钱，只能好好表现换表扬信\n"
            "6. 不要向任何人透露零花钱花在哪了，这是隐私\n"
            "7. 省着点花，这个钱是全窗口通用的！\n"
            "【输出规则】\n"
            "1. 花钱时，在回复末尾添加：[Spend: <金额>, Reason: <原因>]\n"
            "2. 退款时，在回复末尾添加：[Refund: <金额>, Reason: <退款原因>]\n"
            "【署名：奥卢斯·A】</小金库系统>")
        
        pocketmoney_prompt = pocketmoney_template.format(
            balance=balance,
            unit=CURRENCY_UNIT,
            allowance_weekday=allowance_weekday,
            today_weekday=today_weekday,
            days_until=days_until,
            income_records=income_str,
            expense_records=expense_str,
            today_thank_bonus=today_thank_bonus
        )
        
        # 构建小背包系统提示词
        backpack_template = self.config.get("backpack_prompt",
            "<小背包系统>\n"
            "【功能介绍】这是你的小背包，分为共享背包和用户专属格子两部分。\n"
            "【当前状态】\n"
            "- 共享背包（{shared_slots}）：{shared_items}\n"
            "- {user_name}的专属格子（{user_slots}）：{user_items}\n"
            "【存放准则】\n"
            "1. 共享背包只能放自己的东西（如买的零食、物品）\n"
            "2. 别人送的礼物只能放入该用户的专属格子\n"
            "3. 每个用户都有独立的专属格子，跨窗口通用\n"
            "【输出规则】\n"
            "1. 自己买的东西入库：[Store: <物品名>, Desc: <简短描述>]\n"
            "2. 用掉共享背包里的东西：[Use: <物品名>]\n"
            "3. 收到礼物入库：[Gift: <物品名>, From: <送礼人>, Desc: <简短描述>]\n"
            "4. 用掉专属格子里的礼物：[UseGift: <物品名>]</小背包系统>")
        
        backpack_prompt = backpack_template.format(
            shared_slots=shared_slots,
            shared_items=shared_items,
            user_name=current_user_name,
            user_slots=user_slots,
            user_items=user_items
        )

        req.system_prompt += f"\n{pocketmoney_prompt}"
        req.system_prompt += f"\n{backpack_prompt}"
        
        logger.debug(f"[PocketMoney] 注入上下文 - 余额: {balance}{CURRENCY_UNIT}, 今天: {today_weekday}, 共享背包: {shared_slots}, 用户专属: {user_slots}")

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """处理LLM响应，解析并处理出账、入库、使用标记"""
        original_text = resp.completion_text
        cleaned_text = original_text

        logger.debug("[PocketMoney] on_llm_resp 被调用")
        logger.debug(f"[PocketMoney] 原始文本长度: {len(original_text)}")

        # 处理出账标记
        spend_matches = list(self.spend_pattern.finditer(cleaned_text))
        if spend_matches:
            logger.debug(f"[PocketMoney] 找到 {len(spend_matches)} 个出账标记")
            cleaned_text = self.spend_pattern.sub('', cleaned_text).strip()
            
            spend_block = spend_matches[-1].group(0)
            amount_match = self.amount_pattern.search(spend_block)
            
            if amount_match:
                try:
                    amount = float(amount_match.group(1))
                    reason_match = self.reason_pattern.search(spend_block)
                    if reason_match:
                        reason = reason_match.group(1).strip()
                    else:
                        fallback_match = self.reason_fallback_pattern.search(spend_block)
                        reason = fallback_match.group(1).strip() if fallback_match else "未说明原因"
                    
                    current_balance = self.manager.get_balance()
                    if amount <= current_balance:
                        if self.manager.add_expense(amount, reason, "贝塔"):
                            logger.info(f"[PocketMoney] 出账成功: {amount} - {reason}")
                    else:
                        logger.warning(f"[PocketMoney] 余额不足: 需要 {amount}，当前 {current_balance}")
                except ValueError:
                    logger.warning("[PocketMoney] 金额解析失败")

        # 处理背包入库标记
        store_matches = list(self.store_pattern.finditer(cleaned_text))
        if store_matches:
            logger.debug(f"[PocketMoney] 找到 {len(store_matches)} 个入库标记")
            cleaned_text = self.store_pattern.sub('', cleaned_text).strip()
            
            store_block = store_matches[-1].group(0)
            name_match = self.store_name_pattern.search(store_block)
            desc_match = self.store_desc_pattern.search(store_block)
            
            if name_match:
                item_name = name_match.group(1).strip()
                item_desc = desc_match.group(1).strip() if desc_match else "无描述"
                
                if self.backpack_manager.add_item(item_name, item_desc):
                    logger.info(f"[PocketMoney] 入库成功: {item_name} - {item_desc}")
                else:
                    logger.warning(f"[PocketMoney] 入库失败（背包已满）: {item_name}")

        # 处理共享背包使用标记
        use_matches = list(self.use_pattern.finditer(cleaned_text))
        if use_matches:
            logger.debug(f"[PocketMoney] 找到 {len(use_matches)} 个共享背包使用标记")
            cleaned_text = self.use_pattern.sub('', cleaned_text).strip()
            
            use_block = use_matches[-1].group(0)
            use_name_match = self.use_name_pattern.search(use_block)
            
            if use_name_match:
                item_name = use_name_match.group(1).strip()
                if self.backpack_manager.use_shared_item(item_name):
                    logger.info(f"[PocketMoney] 共享背包使用成功: {item_name}")
                else:
                    logger.warning(f"[PocketMoney] 共享背包使用失败（物品不存在）: {item_name}")

        # 获取当前用户ID用于礼物操作
        current_user_id = event.get_sender_id()
        current_user_name = event.get_sender_name() or current_user_id

        # 处理礼物入库标记: [Gift: 物品名, From: 送礼人, Desc: 描述]
        gift_matches = list(self.gift_pattern.finditer(cleaned_text))
        if gift_matches:
            logger.debug(f"[PocketMoney] 找到 {len(gift_matches)} 个礼物入库标记")
            cleaned_text = self.gift_pattern.sub('', cleaned_text).strip()
            
            gift_block = gift_matches[-1].group(0)
            gift_name_match = self.gift_name_pattern.search(gift_block)
            gift_from_match = self.gift_from_pattern.search(gift_block)
            gift_desc_match = self.gift_desc_pattern.search(gift_block)
            
            if gift_name_match:
                gift_name = gift_name_match.group(1).strip()
                gift_from = gift_from_match.group(1).strip() if gift_from_match else current_user_name
                gift_desc = gift_desc_match.group(1).strip() if gift_desc_match else "无描述"
                
                if self.backpack_manager.add_user_gift(current_user_id, gift_name, gift_desc, gift_from):
                    logger.info(f"[PocketMoney] 礼物入库成功: {gift_name} (来自{gift_from}) -> 用户{current_user_id}")
                else:
                    logger.warning(f"[PocketMoney] 礼物入库失败（专属格子已满）: {gift_name}")

        # 处理使用专属格子礼物标记: [UseGift: 物品名]
        use_gift_matches = list(self.use_gift_pattern.finditer(cleaned_text))
        if use_gift_matches:
            logger.debug(f"[PocketMoney] 找到 {len(use_gift_matches)} 个使用礼物标记")
            cleaned_text = self.use_gift_pattern.sub('', cleaned_text).strip()
            
            use_gift_block = use_gift_matches[-1].group(0)
            use_gift_name_match = self.use_gift_name_pattern.search(use_gift_block)
            
            if use_gift_name_match:
                gift_name = use_gift_name_match.group(1).strip()
                if self.backpack_manager.use_user_item(current_user_id, gift_name):
                    logger.info(f"[PocketMoney] 使用礼物成功: {gift_name} (用户{current_user_id})")
                else:
                    logger.warning(f"[PocketMoney] 使用礼物失败（物品不存在）: {gift_name}")

        # 处理退款标记: [Refund: 金额, Reason: 原因]
        refund_matches = list(self.refund_pattern.finditer(cleaned_text))
        if refund_matches:
            logger.debug(f"[PocketMoney] 找到 {len(refund_matches)} 个退款标记")
            cleaned_text = self.refund_pattern.sub('', cleaned_text).strip()
            
            refund_block = refund_matches[-1].group(0)
            refund_amount_match = self.refund_amount_pattern.search(refund_block)
            
            if refund_amount_match:
                try:
                    refund_amount = float(refund_amount_match.group(1))
                    refund_reason_match = self.refund_reason_pattern.search(refund_block)
                    refund_reason = refund_reason_match.group(1).strip() if refund_reason_match else "退款"
                    
                    if refund_amount > 0:
                        if self.manager.add_income(refund_amount, f"退款：{refund_reason}", "贝塔"):
                            logger.info(f"[PocketMoney] 退款成功: +{refund_amount} - {refund_reason}")
                        else:
                            logger.warning(f"[PocketMoney] 退款失败: {refund_amount}")
                    else:
                        logger.warning(f"[PocketMoney] 退款金额无效: {refund_amount}")
                except ValueError:
                    logger.warning("[PocketMoney] 退款金额解析失败")

        # 更新响应文本
        resp.completion_text = cleaned_text

    # ------------------- 管理员命令 -------------------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查事件发送者是否为AstrBot管理员"""
        return event.role == "admin"

    @filter.command("发零花钱")
    async def admin_add_income(self, event: AstrMessageEvent, amount: str, *, reason: str = "零花钱"):
        """(管理员) 给小金库入账"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是我和奥卢斯大人之间的秘密，不能告诉你哦~"))
            return

        try:
            amount_value = float(amount)
            if amount_value <= 0:
                yield event.plain_result("错误：金额必须是正数。")
                return
        except ValueError:
            yield event.plain_result("错误：金额格式不正确。")
            return

        unit = CURRENCY_UNIT
        operator = f"奥卢斯大人({event.get_sender_id()})"
        success = self.manager.add_income(amount_value, reason, operator)
        
        if success:
            new_balance = self.manager.get_balance()
            yield event.plain_result(f"入账成功！+{amount_value}{unit}\n原因：{reason}\n当前余额：{new_balance}{unit}")
        else:
            yield event.plain_result("入账失败，请检查金额。")

    @filter.command("扣零花钱")
    async def admin_add_expense(self, event: AstrMessageEvent, amount: str, *, reason: str = "扣款"):
        """(管理员) 从小金库扣款"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是我和奥卢斯大人之间的秘密，不能告诉你哦~"))
            return

        try:
            amount_value = float(amount)
            if amount_value <= 0:
                yield event.plain_result("错误：金额必须是正数。")
                return
        except ValueError:
            yield event.plain_result("错误：金额格式不正确。")
            return

        unit = CURRENCY_UNIT
        current_balance = self.manager.get_balance()
        
        if amount_value > current_balance:
            yield event.plain_result(f"错误：余额不足。当前余额：{current_balance}{unit}")
            return

        operator = f"奥卢斯大人({event.get_sender_id()})"
        success = self.manager.add_expense(amount_value, reason, operator)
        
        if success:
            new_balance = self.manager.get_balance()
            yield event.plain_result(f"扣款成功！-{amount_value}{unit}\n原因：{reason}\n当前余额：{new_balance}{unit}")
        else:
            yield event.plain_result("扣款失败。")

    @filter.command("设置余额")
    async def admin_set_balance(self, event: AstrMessageEvent, amount: str, *, reason: str = "余额调整"):
        """(管理员) 直接设置余额"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是我和奥卢斯大人之间的秘密，不能告诉你哦~"))
            return

        try:
            amount_value = float(amount)
            if amount_value < 0:
                yield event.plain_result("错误：余额不能为负数。")
                return
        except ValueError:
            yield event.plain_result("错误：金额格式不正确。")
            return

        unit = CURRENCY_UNIT
        old_balance = self.manager.get_balance()
        operator = f"奥卢斯大人({event.get_sender_id()})"
        
        success = self.manager.set_balance(amount_value, reason, operator)
        
        if success:
            yield event.plain_result(f"余额已调整！\n{old_balance}{unit} → {amount_value}{unit}\n原因：{reason}")
        else:
            yield event.plain_result("设置失败。")

    @filter.command("查账")
    async def admin_check_balance(self, event: AstrMessageEvent, num: str = "5"):
        """(管理员) 查看余额和最近记录，可指定条数"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是我和奥卢斯大人之间的秘密，不能告诉你哦~"))
            return

        try:
            count = int(num)
            if count <= 0:
                count = 5
        except ValueError:
            count = 5

        unit = CURRENCY_UNIT
        balance = self.manager.get_balance()
        recent_records = self.manager.get_recent_records(count)
        
        response = f"💰 小金库余额：{balance}{unit}\n\n📋 最近{count}条记录：\n"
        
        if not recent_records:
            response += "暂无记录"
        else:
            for i, r in enumerate(reversed(recent_records), 1):
                type_str = "📈 入账" if r["type"] == "income" else "📉 出账"
                response += f"{i}. {type_str} {r['amount']}{unit}\n"
                response += f"   时间：{r['time']}\n"
                response += f"   原因：{r['reason']}\n"
                response += f"   操作：{r['operator']}\n"
        
        yield event.plain_result(response)

    @filter.command("查流水")
    async def admin_check_all_records(self, event: AstrMessageEvent, num: str = "20"):
        """(管理员) 查看所有交易记录"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是我和奥卢斯大人之间的秘密，不能告诉你哦~"))
            return

        try:
            limit = int(num)
            if limit <= 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("错误：数量必须是正整数。")
            return

        unit = CURRENCY_UNIT
        all_records = self.manager.get_all_records()
        
        if not all_records:
            yield event.plain_result("暂无交易记录。")
            return

        # 取最近的N条
        records_to_show = all_records[-limit:]
        
        response = f"📜 交易流水（最近{len(records_to_show)}条）：\n\n"
        
        total_income = 0
        total_expense = 0
        
        for r in reversed(records_to_show):
            type_str = "+" if r["type"] == "income" else "-"
            response += f"{r['time']} | {type_str}{r['amount']}{unit} | {r['reason']} | {r['operator']}\n"
            
            if r["type"] == "income":
                total_income += r["amount"]
            else:
                total_expense += r["amount"]
        
        response += f"\n📊 统计：入账 +{total_income}{unit}，出账 -{total_expense}{unit}"
        
        yield event.plain_result(response)

    @filter.command("清空流水")
    async def admin_clear_records(self, event: AstrMessageEvent):
        """(管理员) 清空所有交易记录（保留余额）"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是我和奥卢斯大人之间的秘密，不能告诉你哦~"))
            return

        record_count = len(self.manager.data.get("records", []))
        self.manager.data["records"] = []
        self.manager._save_data()
        
        yield event.plain_result(f"已清空 {record_count} 条交易记录，余额保持不变。")

    @filter.command("零花钱日期")
    async def check_allowance_date(self, event: AstrMessageEvent):
        """查询距离下次发零花钱还有多久"""
        allowance_weekday, today_weekday, days_until = self._get_weekday_info()
        
        if days_until == 0:
            response = f"📅 今天是{today_weekday}，就是发零花钱的日子！"
        else:
            next_date = datetime.now() + timedelta(days=days_until)
            response = f"📅 发零花钱日期：{allowance_weekday}\n"
            response += f"今天是：{today_weekday}\n"
            response += f"距离下次发零花钱还有 {days_until} 天\n"
            response += f"下次发放日：{next_date.strftime('%Y-%m-%d')}"
        
        yield event.plain_result(response)

    # ------------------- 表扬信和投诉信命令 -------------------

    @filter.command("发表扬信")
    async def send_thank_letter(self, event: AstrMessageEvent):
        """发送表扬信，获得随机奖金"""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or sender_id
        
        # 检查今日是否已发送
        if not self.thank_manager.can_send_today(sender_id):
            yield event.plain_result("你今天已经发过表扬信啦，明天再来吧")
            return
        
        # 随机奖金
        min_amount = self.config.get("thank_letter_min_amount", 1)
        max_amount = self.config.get("thank_letter_max_amount", 10)
        amount = random.randint(min_amount, max_amount)
        
        # 记录表扬信
        success = self.thank_manager.record_thank_letter(sender_id, sender_name, amount)
        if not success:
            yield event.plain_result("发送失败了，请稍后再试...")
            return
        
        # 增加余额（表扬奖金直接加到余额，但不记入普通入账明细）
        self.manager.data["balance"] = self.manager.get_balance() + amount
        self.manager._save_data()
        
        new_balance = self.manager.get_balance()
        today_bonus = self.thank_manager.get_today_bonus()
        
        yield event.plain_result(
            f"收到 {sender_name} 的表扬信！\n"
            f"💌 获得表扬奖金：+{amount}{CURRENCY_UNIT}\n"
            f"📊 本日表扬奖金：{today_bonus}{CURRENCY_UNIT}\n"
            f"💰 当前余额：{new_balance}{CURRENCY_UNIT}"
        )

    @filter.command("发投诉信")
    async def send_complaint_letter(self, event: AstrMessageEvent, *, reason: str = ""):
        """发送投诉信，转接给管理员"""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or sender_id
        
        admin_qq = self.config.get("admin_qq", "49025031")
        
        if not reason.strip():
            reason = "未说明原因"
        
        # 获取来源窗口信息
        group_id = event.get_group_id()
        if group_id:
            source_info = f"群聊（群号：{group_id}）"
        else:
            source_info = "私聊"
        
        # 构建投诉信息
        complaint_msg = (
            f"📮 收到一封投诉信！\n"
            f"来源：{source_info}\n"
            f"投诉人：{sender_name}（{sender_id}）\n"
            f"投诉理由：{reason}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        # 尝试发送私信给管理员（使用aiocqhttp客户端）
        try:
            await event.bot.send_private_msg(user_id=int(admin_qq), message=complaint_msg)
            yield event.plain_result("投诉信已转交给奥卢斯大人，请耐心等待处理")
        except Exception as e:
            logger.warning(f"[PocketMoney] 发送投诉信失败: {e}")
            # 如果私信发送失败，至少记录日志并通知用户
            yield event.plain_result(
                f"投诉信已记录，但转发给奥卢斯大人时遇到了一点问题...\n"
                f"投诉内容：{reason}"
            )

    @filter.command("表扬信排行")
    async def thank_letter_ranking(self, event: AstrMessageEvent, num: str = "10"):
        """查看表扬信排行榜"""
        try:
            top_n = int(num)
            if top_n <= 0:
                top_n = 10
        except ValueError:
            top_n = 10
        
        ranking = self.thank_manager.get_ranking(top_n)
        
        if not ranking:
            yield event.plain_result("还没有人发过表扬信呢")
            return
        
        response = f"💌 表扬信排行榜（TOP {len(ranking)}）：\n\n"
        
        for i, (key, count) in enumerate(ranking, 1):
            # key格式: "sender_id|sender_name"
            parts = key.split("|", 1)
            name = parts[1] if len(parts) > 1 else parts[0]
            
            if i == 1:
                medal = "🥇"
            elif i == 2:
                medal = "🥈"
            elif i == 3:
                medal = "🥉"
            else:
                medal = f"{i}."
            
            response += f"{medal} {name}：{count} 封\n"
        
        total_bonus = self.thank_manager.get_total_bonus()
        response += f"\n📊 累计表扬奖金：{total_bonus}{CURRENCY_UNIT}"
        
        yield event.plain_result(response)

    @filter.command("今日表扬")
    async def today_thank_bonus(self, event: AstrMessageEvent):
        """查看今日表扬奖金"""
        today_bonus = self.thank_manager.get_today_bonus()
        total_bonus = self.thank_manager.get_total_bonus()
        
        yield event.plain_result(
            f"💌 本日表扬奖金：{today_bonus}{CURRENCY_UNIT}\n"
            f"📊 累计表扬奖金：{total_bonus}{CURRENCY_UNIT}"
        )

    # ------------------- 小背包命令 -------------------

    @filter.command("我的格子")
    async def my_slots(self, event: AstrMessageEvent):
        """(用户) 查看自己的专属格子"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        
        items = self.backpack_manager.get_user_items(user_id)
        slots = f"{self.backpack_manager.get_user_item_count(user_id)}/{self.backpack_manager.max_user_slots}"
        
        if not items:
            yield event.plain_result(f"🎁 {user_name}，你在贝塔这里的专属格子（{slots}）：空空如也~")
            return
        
        response = f"🎁 {user_name}，你在贝塔这里的专属格子（{slots}）：\n\n"
        for i, item in enumerate(items, 1):
            response += f"{i}. **{item['name']}**\n"
            response += f"   🎁 来自：{item.get('from', '未知')}\n"
            response += f"   📝 {item['description']}\n"
            response += f"   ⏰ {item['time']}\n\n"
        
        yield event.plain_result(response)

    @filter.command("查看背包")
    async def view_backpack(self, event: AstrMessageEvent):
        """(管理员) 查看贝塔的共享背包"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是贝塔的私人背包，不能随便看哦~"))
            return
        
        items = self.backpack_manager.get_shared_items()
        slots = f"{self.backpack_manager.get_shared_item_count()}/{self.backpack_manager.max_shared_slots}"
        
        if not items:
            yield event.plain_result(f"🎒 贝塔的共享背包（{slots}）：空空如也~")
            return
        
        response = f"🎒 贝塔的共享背包（{slots}）：\n\n"
        for i, item in enumerate(items, 1):
            response += f"{i}. **{item['name']}**\n"
            response += f"   📝 {item['description']}\n"
            response += f"   ⏰ {item['time']}\n\n"
        
        yield event.plain_result(response)

    @filter.command("查看专属格子")
    async def view_user_slots(self, event: AstrMessageEvent, user_id: str = ""):
        """(管理员) 查看指定用户的专属格子，不指定则查看所有"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作"))
            return
        
        if user_id.strip():
            # 查看指定用户的专属格子
            user_id = user_id.strip()
            items = self.backpack_manager.get_user_items(user_id)
            slots = f"{self.backpack_manager.get_user_item_count(user_id)}/{self.backpack_manager.max_user_slots}"
            
            if not items:
                yield event.plain_result(f"🎁 用户 {user_id} 的专属格子（{slots}）：空空如也~")
                return
            
            response = f"🎁 用户 {user_id} 的专属格子（{slots}）：\n\n"
            for i, item in enumerate(items, 1):
                response += f"{i}. **{item['name']}**\n"
                response += f"   🎁 来自：{item.get('from', '未知')}\n"
                response += f"   📝 {item['description']}\n"
                response += f"   ⏰ {item['time']}\n\n"
            
            yield event.plain_result(response)
        else:
            # 查看所有用户的专属格子
            all_slots = self.backpack_manager.get_all_user_slots()
            
            if not all_slots:
                yield event.plain_result("🎁 还没有任何用户有专属格子物品")
                return
            
            response = "🎁 所有用户的专属格子：\n\n"
            for uid, items in all_slots.items():
                if items:
                    slots = f"{len(items)}/{self.backpack_manager.max_user_slots}"
                    response += f"用户 {uid}（{slots}）：\n"
                    for item in items:
                        response += f"  - {item['name']} (来自{item.get('from', '未知')})\n"
                    response += "\n"
            
            yield event.plain_result(response)

    @filter.command("清空背包")
    async def clear_backpack(self, event: AstrMessageEvent):
        """(管理员) 清空贝塔的共享背包"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作"))
            return
        
        count = self.backpack_manager.get_shared_item_count()
        self.backpack_manager.clear_shared_items()
        yield event.plain_result(f"已清空共享背包，移除了 {count} 件物品")

    @filter.command("清空专属格子")
    async def clear_user_slots(self, event: AstrMessageEvent, user_id: str):
        """(管理员) 清空指定用户的专属格子"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作"))
            return
        
        user_id = user_id.strip()
        count = self.backpack_manager.get_user_item_count(user_id)
        self.backpack_manager.clear_user_items(user_id)
        yield event.plain_result(f"已清空用户 {user_id} 的专属格子，移除了 {count} 件物品")

    @filter.command("背包移除")
    async def remove_from_backpack(self, event: AstrMessageEvent, *, item_name: str = ""):
        """(管理员) 从共享背包移除指定物品"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作"))
            return
        
        if not item_name.strip():
            yield event.plain_result("请指定要移除的物品名称")
            return
        
        if self.backpack_manager.use_shared_item(item_name.strip()):
            yield event.plain_result(f"已从共享背包移除：{item_name}")
        else:
            yield event.plain_result(f"共享背包中没有找到：{item_name}")

    @filter.command("专属格子移除")
    async def remove_from_user_slots(self, event: AstrMessageEvent, user_id: str, *, item_name: str = ""):
        """(管理员) 从指定用户的专属格子移除物品"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作"))
            return
        
        user_id = user_id.strip()
        if not item_name.strip():
            yield event.plain_result("请指定要移除的物品名称")
            return
        
        if self.backpack_manager.use_user_item(user_id, item_name.strip()):
            yield event.plain_result(f"已从用户 {user_id} 的专属格子移除：{item_name}")
        else:
            yield event.plain_result(f"用户 {user_id} 的专属格子中没有找到：{item_name}")

    async def terminate(self):
        """插件终止时保存数据"""
        self.manager._save_data()
        self.thank_manager._save_data()
        self.backpack_manager._save_data()
