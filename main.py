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


class UserIsolationManager:
    """
    用户隔离池管理系统（代理模式）
    - 为每个黑名单用户创建独立的管理器实例，复用现有类
    - 黑名单用户的操作只影响自己的隔离数据
    - 不提示用户进入了黑名单（静默处理）
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.isolation_dir = os.path.join(data_dir, "isolation")
        os.makedirs(self.isolation_dir, exist_ok=True)
        self.blacklist = self._load_blacklist()
        # 缓存已加载的隔离管理器 {user_id: {"money": PocketMoneyManager, "backpack": BackpackManager}}
        self._managers_cache: Dict[str, Dict[str, Any]] = {}

    def _load_blacklist(self) -> List[str]:
        """加载黑名单"""
        path = os.path.join(self.data_dir, "blacklist.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    def _save_blacklist(self):
        """保存黑名单"""
        path = os.path.join(self.data_dir, "blacklist.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.blacklist, f, ensure_ascii=False, indent=2)

    def is_blacklisted(self, user_id: str) -> bool:
        return str(user_id) in self.blacklist

    def add_to_blacklist(self, user_id: str) -> bool:
        user_id = str(user_id)
        if user_id not in self.blacklist:
            self.blacklist.append(user_id)
            self._save_blacklist()
            return True
        return False

    def remove_from_blacklist(self, user_id: str) -> bool:
        user_id = str(user_id)
        if user_id in self.blacklist:
            self.blacklist.remove(user_id)
            self._save_blacklist()
            # 清除缓存
            self._managers_cache.pop(user_id, None)
            return True
        return False

    def get_blacklist(self) -> List[str]:
        return self.blacklist.copy()

    def get_user_isolation_dir(self, user_id: str) -> str:
        """获取用户的隔离数据目录"""
        return os.path.join(self.isolation_dir, str(user_id))

    def get_isolated_managers(self, user_id: str, real_money_mgr: 'PocketMoneyManager', 
                               real_backpack_mgr: 'BackpackManager') -> Dict[str, Any]:
        """
        获取用户的隔离管理器（懒加载，首次访问时复制真实数据）
        返回 {"money": PocketMoneyManager, "backpack": BackpackManager}
        """
        user_id = str(user_id)
        if user_id in self._managers_cache:
            return self._managers_cache[user_id]
        
        user_dir = self.get_user_isolation_dir(user_id)
        os.makedirs(user_dir, exist_ok=True)
        
        # 检查是否首次创建（需要复制真实数据）
        money_file = os.path.join(user_dir, "pocket_money.json")
        if not os.path.exists(money_file):
            # 首次创建，复制当前真实余额
            init_data = {
                "balance": real_money_mgr.get_balance(),
                "records": [],
                "notes": []
            }
            with open(money_file, "w", encoding="utf-8") as f:
                json.dump(init_data, f, ensure_ascii=False, indent=2)
        
        # 创建隔离管理器实例（复用现有类）
        isolated_money = PocketMoneyManager(user_dir, 0, 50)
        isolated_backpack = BackpackManager(user_dir, 10, 3)
        
        self._managers_cache[user_id] = {
            "money": isolated_money,
            "backpack": isolated_backpack
        }
        return self._managers_cache[user_id]

    def _save_data(self):
        """保存所有缓存的管理器数据"""
        for user_id, managers in self._managers_cache.items():
            managers["money"]._save_data()
            managers["backpack"]._save_data()
        self._save_blacklist()

    def sync_expense_to_all(self, amount: float, reason: str, operator_id: str, real_money_mgr: 'PocketMoneyManager'):
        """将出账操作同步到所有隔离池（普通用户操作时调用）"""
        for user_id in self.blacklist:
            managers = self.get_isolated_managers(user_id, real_money_mgr, None)
            mgr = managers["money"]
            if amount <= mgr.get_balance():
                mgr.add_expense(amount, reason, operator_id)

    def sync_income_to_all(self, amount: float, reason: str, operator_id: str, real_money_mgr: 'PocketMoneyManager'):
        """将入账操作同步到所有隔离池（普通用户操作时调用）"""
        for user_id in self.blacklist:
            managers = self.get_isolated_managers(user_id, real_money_mgr, None)
            managers["money"].add_income(amount, reason, operator_id)

    def sync_store_to_all(self, item_name: str, item_desc: str, real_backpack_mgr: 'BackpackManager'):
        """将背包入库同步到所有隔离池"""
        for user_id in self.blacklist:
            managers = self.get_isolated_managers(user_id, None, real_backpack_mgr)
            managers["backpack"].add_shared_item(item_name, item_desc)

    def sync_use_to_all(self, item_name: str, real_backpack_mgr: 'BackpackManager'):
        """将背包使用同步到所有隔离池"""
        for user_id in self.blacklist:
            managers = self.get_isolated_managers(user_id, None, real_backpack_mgr)
            managers["backpack"].use_shared_item(item_name)

    def sync_set_balance_to_all(self, new_balance: float, reason: str, operator_id: str, real_money_mgr: 'PocketMoneyManager'):
        """将设置余额同步到所有隔离池"""
        for user_id in self.blacklist:
            managers = self.get_isolated_managers(user_id, real_money_mgr, None)
            managers["money"].set_balance(new_balance, reason, operator_id)


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
        # 标准化输入名称用于模糊匹配
        normalized_name = name.strip().lower().replace(" ", "").replace("\u3000", "")
        for i, item in enumerate(items):
            # 模糊匹配：忽略空格和大小写
            item_normalized = item["name"].strip().lower().replace(" ", "").replace("\u3000", "")
            if item_normalized == normalized_name or item["name"] == name:
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
        # 标准化输入名称用于模糊匹配
        normalized_name = name.strip().lower().replace(" ", "").replace("\u3000", "")
        for i, item in enumerate(items):
            # 模糊匹配：忽略空格和大小写
            item_normalized = item["name"].strip().lower().replace(" ", "").replace("\u3000", "")
            if item_normalized == normalized_name or item["name"] == name:
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



class PocketMoneyManager:
    """
    小金库管理系统（含存折功能）
    - 全局余额管理（不区分会话）
    - 入账/出账记录
    - 存折：需要审批才能取款的安全余额
    - 笔记功能：贝塔可以自己编辑的备忘录
    """

    def __init__(self, data_dir: str, initial_balance: float = 0, max_records: int = 100):
        self.data_dir = data_dir
        self.initial_balance = initial_balance
        self.max_records = max_records
        self._init_path()
        self.data = self._load_data()
        self._migrate_savings_data()  # 迁移旧的存折数据

    def _init_path(self):
        """初始化数据目录"""
        os.makedirs(self.data_dir, exist_ok=True)

    def _load_data(self) -> Dict[str, Any]:
        """加载金库数据"""
        path = os.path.join(self.data_dir, "pocket_money.json")
        if not os.path.exists(path):
            return {"balance": self.initial_balance, "records": [], "savings_balance": 0, "pending_withdrawals": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "balance" not in data:
                    data["balance"] = self.initial_balance
                if "records" not in data:
                    data["records"] = []
                if "savings_balance" not in data:
                    data["savings_balance"] = 0
                if "pending_withdrawals" not in data:
                    data["pending_withdrawals"] = []
                return data
        except (json.JSONDecodeError, TypeError):
            return {"balance": self.initial_balance, "records": [], "savings_balance": 0, "pending_withdrawals": []}

    def _migrate_savings_data(self):
        """迁移旧的存折数据到小金库"""
        old_path = os.path.join(self.data_dir, "savings_book.json")
        if os.path.exists(old_path):
            try:
                with open(old_path, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    if old_data.get("balance", 0) > 0:
                        self.data["savings_balance"] = old_data.get("balance", 0)
                    if old_data.get("pending_withdrawals"):
                        self.data["pending_withdrawals"] = old_data.get("pending_withdrawals", [])
                    self._save_data()
                    logger.info(f"[PocketMoney] 已迁移旧存折数据: 余额{old_data.get('balance', 0)}元")
            except (json.JSONDecodeError, IOError):
                pass

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

    def get_today_expense(self) -> float:
        """获取今日花销（从凌晨0点开始）"""
        today = datetime.now().strftime("%Y-%m-%d")
        records = self.data.get("records", [])
        total = 0.0
        for r in records:
            if r["type"] == "expense" and r["time"].startswith(today):
                total += r["amount"]
        return total

    def get_all_records(self) -> List[Dict[str, Any]]:
        """获取所有记录"""
        return self.data.get("records", [])

    def add_income(self, amount: float, reason: str, operator_id: str = "") -> bool:
        """
        入账（只能由管理员操作）
        :param amount: 金额（正数）
        :param reason: 原因
        :param operator_id: 操作人QQ号
        :return: 是否成功
        """
        if amount <= 0:
            return False
        
        self.data["balance"] = round(self.get_balance() + amount, 2)
        record = {
            "type": "income",
            "amount": amount,
            "reason": reason,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator_id": operator_id
        }
        self.data["records"].append(record)
        
        # 限制记录数量
        if len(self.data["records"]) > self.max_records:
            self.data["records"] = self.data["records"][-self.max_records:]
        
        self._save_data()
        return True

    def add_expense(self, amount: float, reason: str, operator_id: str = "") -> bool:
        """
        出账（AI自主或管理员操作）
        :param amount: 金额（正数）
        :param reason: 原因
        :param operator_id: 操作人QQ号（AI操作时为触发者QQ号）
        :return: 是否成功
        """
        if amount <= 0:
            return False
        if amount > self.get_balance():
            return False
        
        self.data["balance"] = round(self.get_balance() - amount, 2)
        record = {
            "type": "expense",
            "amount": amount,
            "reason": reason,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator_id": operator_id
        }
        self.data["records"].append(record)
        
        # 限制记录数量
        if len(self.data["records"]) > self.max_records:
            self.data["records"] = self.data["records"][-self.max_records:]
        
        self._save_data()
        return True

    def set_balance(self, amount: float, reason: str, operator_id: str = "") -> bool:
        """
        直接设置余额（管理员操作）
        :param operator_id: 操作人QQ号
        """
        old_balance = self.get_balance()
        self.data["balance"] = round(amount, 2)
        
        diff = amount - old_balance
        record_type = "income" if diff >= 0 else "expense"
        record = {
            "type": record_type,
            "amount": abs(diff),
            "reason": f"[余额调整] {reason}",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator_id": operator_id
        }
        self.data["records"].append(record)
        
        if len(self.data["records"]) > self.max_records:
            self.data["records"] = self.data["records"][-self.max_records:]
        
        self._save_data()
        return True

    # ========== 笔记功能 ==========
    
    def get_notes(self) -> list:
        """获取笔记列表"""
        notes = self.data.get("notes", [])
        # 兼容旧版本单字符串格式
        if not notes and self.data.get("note"):
            return [self.data.get("note")]
        return notes
    
    def get_note(self) -> str:
        """获取格式化的笔记内容（用于提示词）"""
        notes = self.get_notes()
        if not notes:
            return ""
        # 返回格式化的笔记列表
        return "\n".join([f"{i+1}. {note}" for i, note in enumerate(notes)])
    
    def add_note(self, content: str, max_entries: int = 5) -> bool:
        """
        添加笔记条目（自动限制数量）
        :param content: 笔记内容
        :param max_entries: 最大保留条数
        :return: 是否成功
        """
        content = content.strip()
        if not content:
            return False
        
        notes = self.data.get("notes", [])
        # 兼容旧版本：迁移旧的单字符串笔记
        if not notes and self.data.get("note"):
            notes = [self.data.get("note")]
            self.data.pop("note", None)
        
        notes.append(content)
        
        # 限制数量，删除最旧的
        if len(notes) > max_entries:
            notes = notes[-max_entries:]
        
        self.data["notes"] = notes
        self._save_data()
        return True
    
    def set_note(self, content: str, max_entries: int = 5) -> bool:
        """
        设置笔记（兼容旧接口，实际调用add_note）
        """
        return self.add_note(content, max_entries)
    
    def clear_notes(self) -> bool:
        """清空所有笔记"""
        self.data["notes"] = []
        self.data.pop("note", None)  # 清理旧格式
        self._save_data()
        return True
    
    def clear_note(self) -> bool:
        """清空笔记（兼容旧接口）"""
        return self.clear_notes()
    
    def delete_note(self, index: int) -> bool:
        """
        删除指定索引的笔记条目（1-indexed）
        :param index: 笔记序号（从1开始）
        :return: 是否成功
        """
        notes = self.get_notes()
        if not notes:
            return False
        
        # 转换为0-indexed
        idx = index - 1
        if idx < 0 or idx >= len(notes):
            return False
        
        # 确保 notes 是列表格式
        if "notes" not in self.data:
            self.data["notes"] = notes
        
        self.data["notes"].pop(idx)
        self._save_data()
        return True

    # ========== 存折功能（需审批的安全余额） ==========
    
    def get_savings_balance(self) -> float:
        """获取存折余额"""
        return self.data.get("savings_balance", 0)

    def deposit_to_savings(self, amount: float, reason: str, operator_id: str = "") -> bool:
        """存入存折（从小金库转入）"""
        if amount <= 0 or amount > self.get_balance():
            return False
        self.data["balance"] = round(self.get_balance() - amount, 2)
        self.data["savings_balance"] = round(self.get_savings_balance() + amount, 2)
        self.data["records"].append({
            "type": "expense", "amount": amount,
            "reason": f"[转入存折] {reason}",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator_id": operator_id
        })
        self._save_data()
        return True

    def withdraw_from_savings(self, amount: float, reason: str, operator_id: str = "") -> bool:
        """从存折取款（管理员直接操作，转入小金库）"""
        if amount <= 0 or amount > self.get_savings_balance():
            return False
        self.data["savings_balance"] = round(self.get_savings_balance() - amount, 2)
        self.data["balance"] = round(self.get_balance() + amount, 2)
        self.data["records"].append({
            "type": "income", "amount": amount,
            "reason": f"[存折取款] {reason}",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator_id": operator_id
        })
        self._save_data()
        return True

    def apply_withdrawal(self, amount: float, reason: str, source_info: dict = None) -> str:
        """申请取款（AI发起，等待管理员审批）"""
        if amount <= 0 or amount > self.get_savings_balance():
            return ""
        import time
        application_id = f"{int(time.time())}{random.randint(100, 999)}"
        self.data["pending_withdrawals"].append({
            "id": application_id, "amount": amount, "reason": reason,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending", "source_info": source_info or {}
        })
        self._save_data()
        return application_id

    def get_pending_withdrawals(self) -> List[Dict[str, Any]]:
        """获取待审批的取款申请"""
        return [w for w in self.data.get("pending_withdrawals", []) if w.get("status") == "pending"]

    def approve_withdrawal(self, application_id: str, operator_id: str = "") -> tuple:
        """批准取款申请，返回 (成功, 金额, 原因, 来源信息)"""
        for w in self.data.get("pending_withdrawals", []):
            if w.get("id") == application_id and w.get("status") == "pending":
                amount, reason = w.get("amount", 0), w.get("reason", "")
                if amount > self.get_savings_balance():
                    return (False, 0, "余额不足", {})
                w["status"] = "approved"
                w["approved_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.withdraw_from_savings(amount, f"[申请取款] {reason}", operator_id)
                return (True, amount, reason, w.get("source_info", {}))
        return (False, 0, "申请不存在或已处理", {})

    def reject_withdrawal(self, application_id: str, reject_reason: str = "", operator_id: str = "") -> tuple:
        """拒绝取款申请，返回 (成功, 金额, 原因, 来源信息)"""
        for w in self.data.get("pending_withdrawals", []):
            if w.get("id") == application_id and w.get("status") == "pending":
                w["status"] = "rejected"
                w["reject_reason"] = reject_reason
                self._save_data()
                return (True, w.get("amount", 0), w.get("reason", ""), w.get("source_info", {}))
        return (False, 0, "申请不存在或已处理", {})


@register("astrbot_plugin_pocketmoney", "柯尔", "贝塔的小金库系统，管理余额和收支记录", "1.7.0")
# ==================== 版本历史 ====================
# v1.0 - 基础零花钱：余额管理、入账/出账、记录查询
# v1.1 - 表扬信/投诉信系统：每日限制、排行榜、随机奖金 
# v1.2 - 背包系统：共享背包、物品入库/使用
# v1.3 - 专属背包格子：每个用户独立的礼物存储空间
# v1.4 - 笔记功能：AI私密备忘录，管理员可查看/追加
# v1.5 - 数据目录迁移至plugin_data，记录操作窗口source替代operator
# v1.6 - 存折系统：奥卢斯大人保管的钱，AI申请取款需审批
# v1.7 - 代码重构：删除压岁钱系统，合并存折到小金库，移除硬编码提示词
# ==================================================
class PocketMoneyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 使用插件数据目录（按AstrBot规则使用插件注册名）
        self.data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_pocketmoney")
        
        # 自动数据迁移：从旧目录迁移到新目录
        self._migrate_data_if_needed()
        
        initial_balance = self.config.get("initial_balance", 0)
        max_records = self.config.get("max_records", 100)
        
        self.manager = PocketMoneyManager(self.data_dir, initial_balance, max_records)
        
        # 表扬信管理器
        self.thank_manager = ThankLetterManager(self.data_dir)
        
        # 小背包管理器
        max_shared_slots = self.config.get("max_shared_slots", 10)
        max_user_slots = self.config.get("max_user_slots", 3)
        self.backpack_manager = BackpackManager(self.data_dir, max_shared_slots, max_user_slots)
        
        # 用户隔离池管理器（黑名单用户的操作进入隔离池）
        self.isolation_manager = UserIsolationManager(self.data_dir)
        
        # 从配置中加载黑名单用户（与文件中的黑名单合并）
        config_blacklist = self.config.get("blacklist_users", [])
        for uid in config_blacklist:
            self.isolation_manager.add_to_blacklist(str(uid))

        # 匹配出账标记的正则表达式
        self.spend_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:Spend|花费|支出))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.amount_pattern = re.compile(r"(?:Spend|花费|支出)\s*[:：]\s*(\d+(?:\.\d+)?)")
        self.reason_pattern = re.compile(r"(?:Reason|原因|用途)\s*[:：]\s*(.+?)(?=\s*[,，\]]|\])")
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
        
        # 匹配背包使用标记: [Use: 物品名] - 排除UseGift
        self.use_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:(?<!e)Use(?!Gift)|使用(?!礼物)|用掉))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.use_name_pattern = re.compile(r"(?<!e)(?:Use)(?!Gift)\s*[:：]\s*(.+?)(?=\s*\])|(?:使用)(?!礼物)\s*[:：]\s*(.+?)(?=\s*\])|(?:用掉)\s*[:：]\s*(.+?)(?=\s*\])", re.IGNORECASE)
        
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
        
        # 匹配笔记标记: [Note: 内容] 或 [笔记: 内容]
        self.note_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:Note|笔记|备忘|记录))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.note_content_pattern = re.compile(r"(?:Note|笔记|备忘|记录)\s*[:：]\s*(.+?)(?=\s*\])")
        
        # 匹配申请取款标记: [ApplyWithdraw: 金额, Reason: 原因]
        self.apply_withdraw_pattern = re.compile(
            r"\s*\[(?=[^\]]*(?:ApplyWithdraw|申请取款|取存折))[^\]]*\]\s*",
            re.IGNORECASE | re.DOTALL
        )
        self.apply_withdraw_amount_pattern = re.compile(r"(?:ApplyWithdraw|申请取款|取存折)\s*[:：]\s*(\d+(?:\.\d+)?)")
        self.apply_withdraw_reason_pattern = re.compile(r"(?:Reason|原因|理由)\s*[:：]\s*(.+?)(?=\s*[,，\]]|\])")
        self.apply_withdraw_reason_fallback_pattern = re.compile(
            r"(?:ApplyWithdraw|申请取款|取存折)\s*[:：]\s*\d+(?:\.\d+)?\s*[,，]\s*(.+?)(?=\s*\])"
        )
        
        # 防重复扣费：记录已处理的消息ID
        self.processed_message_ids = set()

    def _migrate_data_if_needed(self):
        """从旧数据目录迁移到新目录"""
        import shutil
        
        # 支持多个旧目录（按优先级顺序）
        old_dirs = [
            os.path.join("data", "PocketMoney"),  # 最早的目录
            os.path.join("data", "plugin_data", "PocketMoney"),  # 之前的迁移目录
        ]
        
        # 检查新目录是否已有数据
        new_files = os.listdir(self.data_dir) if os.path.exists(self.data_dir) else []
        if new_files:
            logger.debug("[PocketMoney] 新目录已有数据，跳过迁移")
            return
        
        # 尝试从旧目录迁移
        for old_data_dir in old_dirs:
            if os.path.exists(old_data_dir) and os.path.isdir(old_data_dir):
                old_files = os.listdir(old_data_dir)
                if old_files:
                    os.makedirs(self.data_dir, exist_ok=True)
                    logger.info(f"[PocketMoney] 检测到旧数据目录，开始迁移: {old_data_dir} -> {self.data_dir}")
                    for filename in old_files:
                        old_path = os.path.join(old_data_dir, filename)
                        new_path = os.path.join(self.data_dir, filename)
                        if os.path.isfile(old_path):
                            shutil.copy2(old_path, new_path)
                            logger.info(f"[PocketMoney] 迁移文件: {filename}")
                    logger.info(f"[PocketMoney] 数据迁移完成，旧目录保留供备份: {old_data_dir}")
                    return  # 迁移成功后退出

    def _get_managers_for_user(self, user_id: str) -> tuple:
        """
        获取用户对应的管理器（代理模式核心）
        返回 (money_manager, backpack_manager, is_isolated)
        """
        if self.isolation_manager.is_blacklisted(user_id):
            managers = self.isolation_manager.get_isolated_managers(
                user_id, self.manager, self.backpack_manager
            )
            return managers["money"], managers["backpack"], True
        return self.manager, self.backpack_manager, False

    def _format_records(self, records: List[Dict[str, Any]], show_type: bool = True) -> str:
        """格式化记录为字符串"""
        if not records:
            return "暂无"
        
        lines = []
        for r in records:
            if show_type:
                type_str = "+" if r["type"] == "income" else "-"
                lines.append(f"{r['time']}: {type_str}{r['amount']}元 ({r['reason']})")
            else:
                lines.append(f"{r['time']}: {r['amount']}元 ({r['reason']})")
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
        # 获取当前用户ID
        current_user_id = event.get_sender_id()
        current_user_name = event.get_sender_name() or current_user_id
        
        # 使用代理模式获取正确的管理器（真实或隔离）
        money_mgr, backpack_mgr, is_isolated = self._get_managers_for_user(current_user_id)
        
        if is_isolated:
            logger.debug(f"[PocketMoney] 黑名单用户 {current_user_id} 使用隔离池数据")
        
        # 统一使用获取到的管理器（不管是真实的还是隔离的）
        balance = money_mgr.get_balance()
        income_count = self.config.get("income_record_count", 2)
        expense_count = self.config.get("expense_record_count", 5)
        income_records = money_mgr.get_recent_income_records(income_count)
        expense_records = money_mgr.get_recent_expense_records(expense_count)
        today_expense = money_mgr.get_today_expense()
        
        # 背包信息
        shared_items = backpack_mgr.format_shared_items_for_prompt()
        shared_slots = f"{backpack_mgr.get_shared_item_count()}/{backpack_mgr.max_shared_slots}"
        user_items = backpack_mgr.format_user_items_for_prompt(current_user_id)
        user_slots = f"{backpack_mgr.get_user_item_count(current_user_id)}/{backpack_mgr.max_user_slots}"
        
        # 存折信息（黑名单用户看到隔离池存折，实际上永远是0）
        if is_isolated:
            savings_balance = 0  # 隔离用户没有存折功能
            pending_count = 0
        else:
            savings_balance = self.manager.get_savings_balance()
            pending_count = len(self.manager.get_pending_withdrawals())
        
        income_str = self._format_records(income_records, show_type=False)
        expense_str = self._format_records(expense_records, show_type=False)
        
        # 获取星期信息
        allowance_weekday, today_weekday, days_until = self._get_weekday_info()
        
        # 获取今日表扬奖金
        today_thank_bonus = self.thank_manager.get_today_bonus()
        
        # 获取小金库笔记（黑名单用户使用隔离池笔记）
        note = money_mgr.get_note()

        # 构建存折待审批信息
        pending_info = f"（有{pending_count}个待审批申请）" if pending_count > 0 else ""
        
        # 构建小金库+存折系统提示词（v1.7合并版）
        pocketmoney_template = self.config.get("pocketmoney_prompt", "")
        note_str = f"\n【我的笔记】{note}" if note else ""
        
        pocketmoney_prompt = pocketmoney_template.format(
            balance=balance,
            savings_balance=savings_balance,
            pending_info=pending_info,
            unit="元",
            allowance_weekday=allowance_weekday,
            today_weekday=today_weekday,
            days_until=days_until,
            income_records=income_str,
            expense_records=expense_str,
            today_thank_bonus=today_thank_bonus,
            today_expense=today_expense
        )
        
        # 将笔记插入到 </小金库系统> 标签之前
        if note_str and "</小金库系统>" in pocketmoney_prompt:
            pocketmoney_prompt = pocketmoney_prompt.replace("</小金库系统>", f"{note_str}\n</小金库系统>")
        
        # 构建小背包系统提示词
        backpack_template = self.config.get("backpack_prompt", "")
        backpack_prompt = backpack_template.format(
            shared_slots=shared_slots,
            shared_items=shared_items,
            user_name=current_user_name,
            user_slots=user_slots,
            user_items=user_items
        )

        req.system_prompt += f"\n{pocketmoney_prompt}"
        req.system_prompt += f"\n{backpack_prompt}"
        
        logger.debug(f"[PocketMoney] 注入上下文 - 余额: {balance}元, 存折: {savings_balance}元, 今天: {today_weekday}, 共享背包: {shared_slots}, 用户专属: {user_slots}")

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """处理LLM响应，解析并处理出账、入库、使用标记"""
        original_text = resp.completion_text
        cleaned_text = original_text

        logger.debug("[PocketMoney] on_llm_resp 被调用")
        logger.debug(f"[PocketMoney] 原始文本长度: {len(original_text)}")
        
        # 防重复处理：使用消息ID + 响应文本哈希作为唯一标识
        message_id = getattr(event, 'message_id', None) or id(event)
        response_hash = hash(original_text[:100]) if original_text else 0
        unique_key = f"{message_id}_{response_hash}"
        
        if unique_key in self.processed_message_ids:
            logger.debug(f"[PocketMoney] 跳过重复处理: {unique_key}")
            return
        
        self.processed_message_ids.add(unique_key)
        if len(self.processed_message_ids) > 1000:
            self.processed_message_ids = set(list(self.processed_message_ids)[-500:])

        current_user_id = event.get_sender_id()
        current_user_name = event.get_sender_name() or current_user_id
        
        # 使用代理模式获取正确的管理器（真实或隔离）
        money_mgr, backpack_mgr, is_isolated = self._get_managers_for_user(current_user_id)
        if is_isolated:
            logger.debug(f"[PocketMoney] 黑名单用户 {current_user_id} 的操作将进入隔离池")
        log_prefix = "[隔离池] " if is_isolated else ""

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
                    
                    current_balance = money_mgr.get_balance()
                    if amount <= current_balance:
                        if money_mgr.add_expense(amount, reason, current_user_id):
                            logger.info(f"[PocketMoney] {log_prefix}出账成功: {amount} - {reason}")
                            # 普通用户操作时，同步到所有隔离池
                            if not is_isolated:
                                self.isolation_manager.sync_expense_to_all(amount, reason, current_user_id, self.manager)
                    else:
                        # 保底策略：余额不足时，扣除全部余额并记录
                        if current_balance > 0:
                            fallback_reason = f"{reason}（原请求{amount}元，余额不足，已扣除全部）"
                            money_mgr.add_expense(current_balance, fallback_reason, current_user_id)
                            logger.info(f"[PocketMoney] {log_prefix}保底出账: {current_balance}/{amount} - {reason}")
                            if not is_isolated:
                                self.isolation_manager.sync_expense_to_all(current_balance, fallback_reason, current_user_id, self.manager)
                        else:
                            logger.warning(f"[PocketMoney] {log_prefix}余额为0，无法扣款: {amount} - {reason}")
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
                
                if backpack_mgr.add_shared_item(item_name, item_desc):
                    logger.info(f"[PocketMoney] {log_prefix}入库成功: {item_name} - {item_desc}")
                    if not is_isolated:
                        self.isolation_manager.sync_store_to_all(item_name, item_desc, self.backpack_manager)
                else:
                    logger.warning(f"[PocketMoney] 入库失败（背包已满）: {item_name}")

        # 【重要】先处理UseGift，再处理Use，避免Use误匹配UseGift
        use_gift_matches = list(self.use_gift_pattern.finditer(cleaned_text))
        if use_gift_matches:
            logger.debug(f"[PocketMoney] 找到 {len(use_gift_matches)} 个使用礼物标记")
            cleaned_text = self.use_gift_pattern.sub('', cleaned_text).strip()
            
            for use_gift_block_match in use_gift_matches:
                use_gift_block = use_gift_block_match.group(0)
                use_gift_name_match = self.use_gift_name_pattern.search(use_gift_block)
                
                if use_gift_name_match:
                    gift_name = use_gift_name_match.group(1).strip()
                    if backpack_mgr.use_user_item(current_user_id, gift_name):
                        logger.info(f"[PocketMoney] {log_prefix}使用礼物成功: {gift_name}")
                    else:
                        logger.warning(f"[PocketMoney] 使用礼物失败（物品不存在）: {gift_name}")

        # 处理共享背包使用标记: [Use: 物品名]
        use_matches = list(self.use_pattern.finditer(cleaned_text))
        if use_matches:
            logger.debug(f"[PocketMoney] 找到 {len(use_matches)} 个共享背包使用标记")
            cleaned_text = self.use_pattern.sub('', cleaned_text).strip()
            
            for use_block_match in use_matches:
                use_block = use_block_match.group(0)
                use_name_match = self.use_name_pattern.search(use_block)
                
                if use_name_match:
                    item_name = next((g.strip() for g in use_name_match.groups() if g), None)
                    if item_name:
                        if backpack_mgr.use_shared_item(item_name):
                            logger.info(f"[PocketMoney] {log_prefix}共享背包使用成功: {item_name}")
                            if not is_isolated:
                                self.isolation_manager.sync_use_to_all(item_name, self.backpack_manager)
                        else:
                            logger.warning(f"[PocketMoney] 共享背包使用失败（物品不存在）: {item_name}")

        # 处理礼物入库标记: [Gift: 物品名, From: 送礼人, Desc: 描述]
        gift_matches = list(self.gift_pattern.finditer(cleaned_text))
        if gift_matches:
            logger.debug(f"[PocketMoney] 找到 {len(gift_matches)} 个礼物入库标记")
            cleaned_text = self.gift_pattern.sub('', cleaned_text).strip()
            
            for gift_block_match in gift_matches:
                gift_block = gift_block_match.group(0)
                gift_name_match = self.gift_name_pattern.search(gift_block)
                gift_from_match = self.gift_from_pattern.search(gift_block)
                gift_desc_match = self.gift_desc_pattern.search(gift_block)
                
                if gift_name_match:
                    gift_name = gift_name_match.group(1).strip()
                    gift_from = gift_from_match.group(1).strip() if gift_from_match else current_user_name
                    gift_desc = gift_desc_match.group(1).strip() if gift_desc_match else "无描述"
                    
                    if backpack_mgr.add_user_gift(current_user_id, gift_name, gift_desc, gift_from):
                        logger.info(f"[PocketMoney] {log_prefix}礼物入库成功: {gift_name} (来自{gift_from})")
                    else:
                        logger.warning(f"[PocketMoney] 礼物入库失败（专属格子已满）: {gift_name}")

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
                        refund_full_reason = f"退款：{refund_reason}"
                        if money_mgr.add_income(refund_amount, refund_full_reason, current_user_id):
                            logger.info(f"[PocketMoney] {log_prefix}退款成功: +{refund_amount} - {refund_reason}")
                            if not is_isolated:
                                self.isolation_manager.sync_income_to_all(refund_amount, refund_full_reason, current_user_id, self.manager)
                except ValueError:
                    logger.warning("[PocketMoney] 退款金额解析失败")

        # 处理笔记标记（已禁用自动追加，仅清除标记）
        note_matches = list(self.note_pattern.finditer(cleaned_text))
        if note_matches:
            cleaned_text = self.note_pattern.sub('', cleaned_text).strip()

        # 处理申请取款标记: [ApplyWithdraw: 金额, Reason: 原因]
        apply_withdraw_matches = list(self.apply_withdraw_pattern.finditer(cleaned_text))
        if apply_withdraw_matches:
            logger.debug(f"[PocketMoney] 找到 {len(apply_withdraw_matches)} 个申请取款标记")
            cleaned_text = self.apply_withdraw_pattern.sub('', cleaned_text).strip()
            
            apply_block = apply_withdraw_matches[-1].group(0)
            amount_match = self.apply_withdraw_amount_pattern.search(apply_block)
            
            if amount_match:
                try:
                    amount = float(amount_match.group(1))
                    reason_match = self.apply_withdraw_reason_pattern.search(apply_block)
                    if reason_match:
                        reason = reason_match.group(1).strip()
                    else:
                        fallback_match = self.apply_withdraw_reason_fallback_pattern.search(apply_block)
                        reason = fallback_match.group(1).strip() if fallback_match else "未说明原因"
                    
                    if is_isolated:
                        logger.info(f"[PocketMoney] [隔离池] 申请取款被静默处理: {amount}元 - {reason}")
                    else:
                        savings_balance = self.manager.get_savings_balance()
                        if amount <= savings_balance:
                            group_id = event.get_group_id()
                            source_info = {
                                "group_id": group_id,
                                "is_group": bool(group_id),
                                "user_id": current_user_id
                            }
                            application_id = self.manager.apply_withdrawal(amount, reason, source_info)
                            if application_id:
                                logger.info(f"[PocketMoney] 申请取款成功: {amount}元 - {reason} (申请ID: {application_id})")
                                admin_qq = self.config.get("admin_qq", "49025031")
                                try:
                                    notify_msg = (
                                        f"📋 存折取款申请\n"
                                        f"申请ID：{application_id}\n"
                                        f"金额：{amount}元\n"
                                        f"原因：{reason}\n"
                                        f"存折余额：{savings_balance}元\n"
                                        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                                        f"回复「批准取款 {application_id}」批准\n"
                                        f"回复「拒绝取款 {application_id} 原因」拒绝"
                                    )
                                    await event.bot.send_private_msg(user_id=int(admin_qq), message=notify_msg)
                                except Exception as e:
                                    logger.warning(f"[PocketMoney] 通知管理员失败: {e}")
                        else:
                            logger.warning(f"[PocketMoney] 存折余额不足: 需要 {amount}，当前 {savings_balance}")
                except ValueError:
                    logger.warning("[PocketMoney] 申请取款金额解析失败")

        resp.completion_text = cleaned_text

    # ------------------- 管理员命令 -------------------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.role == "admin"
    
    def _admin_denied_msg(self):
        return self.config.get("admin_permission_denied_msg", "这是我和奥卢斯大人之间的秘密，不能告诉你哦~")
    
    def _parse_amount(self, amount_str: str, allow_zero: bool = False) -> tuple:
        """解析金额，返回 (成功, 金额或错误信息)"""
        try:
            val = float(amount_str)
            if val < 0 or (val == 0 and not allow_zero):
                return (False, "金额必须是正数")
            return (True, val)
        except ValueError:
            return (False, "金额格式不正确")

    @filter.command("发零花钱")
    async def admin_add_income(self, event: AstrMessageEvent, amount: str, *, reason: str = "零花钱"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        if self.manager.add_income(val, reason, event.get_sender_id()):
            # 同步到所有隔离池
            self.isolation_manager.sync_income_to_all(val, reason, event.get_sender_id(), self.manager)
            yield event.plain_result(f"入账成功！+{val}元\n原因：{reason}\n当前余额：{self.manager.get_balance()}元")

    @filter.command("扣零花钱")
    async def admin_add_expense(self, event: AstrMessageEvent, amount: str, *, reason: str = "扣款"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        if val > self.manager.get_balance():
            yield event.plain_result(f"错误：余额不足。当前余额：{self.manager.get_balance()}元"); return
        if self.manager.add_expense(val, reason, event.get_sender_id()):
            # 同步到所有隔离池
            self.isolation_manager.sync_expense_to_all(val, reason, event.get_sender_id(), self.manager)
            yield event.plain_result(f"扣款成功！-{val}元\n原因：{reason}\n当前余额：{self.manager.get_balance()}元")

    @filter.command("设置余额")
    async def admin_set_balance(self, event: AstrMessageEvent, amount: str, *, reason: str = "余额调整"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        ok, val = self._parse_amount(amount, allow_zero=True)
        if not ok:
            yield event.plain_result(f"错误：{val}"); return
        old = self.manager.get_balance()
        if self.manager.set_balance(val, reason, event.get_sender_id()):
            # 同步到所有隔离池
            self.isolation_manager.sync_set_balance_to_all(val, reason, event.get_sender_id(), self.manager)
            yield event.plain_result(f"余额已调整！\n{old}元 → {val}元\n原因：{reason}")

    @filter.command("查账")
    async def admin_check_balance(self, event: AstrMessageEvent, num: str = "5"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        count = max(1, int(num)) if num.isdigit() else 5
        records = self.manager.get_recent_records(count)
        response = f"💰 小金库余额：{self.manager.get_balance()}元\n\n📋 最近{count}条记录：\n"
        if not records:
            response += "暂无记录"
        else:
            for i, r in enumerate(reversed(records), 1):
                t = "📈 入账" if r["type"] == "income" else "📉 出账"
                response += f"{i}. {t} {r['amount']}元 | {r['time']} | {r['reason']}\n"
        yield event.plain_result(response)

    @filter.command("查流水")
    async def admin_check_all_records(self, event: AstrMessageEvent, num: str = "20"):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        limit = max(1, int(num)) if num.isdigit() else 20
        all_records = self.manager.get_all_records()
        if not all_records:
            yield event.plain_result("暂无交易记录。"); return
        records = all_records[-limit:]
        response = f"📜 交易流水（最近{len(records)}条）：\n\n"
        total_income, total_expense = 0, 0
        for r in reversed(records):
            t = "+" if r["type"] == "income" else "-"
            operator_id = r.get("operator_id", "")
            operator_str = f" | @{operator_id}" if operator_id else ""
            response += f"{r['time']} | {t}{r['amount']}元 | {r['reason']}{operator_str}\n"
            if r["type"] == "income": total_income += r["amount"]
            else: total_expense += r["amount"]
        response += f"\n📊 统计：入账 +{total_income}元，出账 -{total_expense}元"
        yield event.plain_result(response)

    @filter.command("清空流水")
    async def admin_clear_records(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._admin_denied_msg()); return
        count = len(self.manager.data.get("records", []))
        self.manager.data["records"] = []
        self.manager._save_data()
        yield event.plain_result(f"已清空 {count} 条交易记录，余额保持不变。")

    @filter.command("零花钱日期")
    async def check_allowance_date(self, event: AstrMessageEvent):
        wd, today, days = self._get_weekday_info()
        if days == 0:
            yield event.plain_result(f"📅 今天是{today}，就是发零花钱的日子！")
        else:
            yield event.plain_result(f"📅 发零花钱日：{wd}\n今天：{today}\n还有 {days} 天")

    # ------------------- 表扬信和投诉信命令 -------------------

    @filter.command("发表扬信")
    async def send_thank_letter(self, event: AstrMessageEvent):
        uid, name = event.get_sender_id(), event.get_sender_name() or event.get_sender_id()
        money_mgr, _, is_isolated = self._get_managers_for_user(uid)
        log_prefix = "[隔离池] " if is_isolated else ""
        
        if not self.thank_manager.can_send_today(uid):
            yield event.plain_result("你今天已经发过表扬信啦，明天再来吧")
            return
        amount = random.randint(self.config.get("thank_letter_min_amount", 1), self.config.get("thank_letter_max_amount", 10))
        if not self.thank_manager.record_thank_letter(uid, name, amount):
            yield event.plain_result("发送失败了，请稍后再试...")
            return
        # 使用代理管理器（黑名单用户进入隔离池）
        money_mgr.data["balance"] = round(money_mgr.get_balance() + amount, 2)
        money_mgr._save_data()
        logger.info(f"[PocketMoney] {log_prefix}表扬信奖金: +{amount}元")
        yield event.plain_result(
            f"收到 {name} 的表扬信！\n"
            f"🎉 获得表扬奖金：+{amount}元\n"
            f"📊 本日表扬奖金：{self.thank_manager.get_today_bonus()}元\n"
            f"💰 当前余额：{money_mgr.get_balance()}元"
        )

    @filter.command("发投诉信")
    async def send_complaint_letter(self, event: AstrMessageEvent, *, reason: str = ""):
        uid, name = event.get_sender_id(), event.get_sender_name() or event.get_sender_id()
        reason = reason.strip() or "未说明原因"
        src = f"群{event.get_group_id()}" if event.get_group_id() else "私聊"
        msg = f"📮 投诉信！\n来源：{src}\n投诉人：{name}({uid})\n理由：{reason}"
        try:
            await event.bot.send_private_msg(user_id=int(self.config.get("admin_qq", "49025031")), message=msg)
            yield event.plain_result("投诉信已转交给奥卢斯大人")
        except: yield event.plain_result(f"投诉已记录：{reason}")

    @filter.command("表扬信排行")
    async def thank_letter_ranking(self, event: AstrMessageEvent, num: str = "10"):
        top_n = max(1, int(num)) if num.isdigit() else 10
        ranking = self.thank_manager.get_ranking(top_n)
        if not ranking:
            yield event.plain_result("还没有人发过表扬信呢"); return
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"💌 表扬信排行榜（TOP {len(ranking)}）：\n"]
        for i, (key, count) in enumerate(ranking, 1):
            name = key.split("|", 1)[-1]
            m = medals[i-1] if i <= 3 else f"{i}."
            lines.append(f"{m} {name}：{count} 封")
        lines.append(f"\n📊 累计奖金：{self.thank_manager.get_total_bonus()}元")
        yield event.plain_result("\n".join(lines))

    @filter.command("今日表扬")
    async def today_thank_bonus(self, event: AstrMessageEvent):
        yield event.plain_result(f"💌 本日：{self.thank_manager.get_today_bonus()}元\n📊 累计：{self.thank_manager.get_total_bonus()}元")

    # ------------------- 小背包命令 -------------------

    @filter.command("我的格子")
    async def my_slots(self, event: AstrMessageEvent):
        """(用户) 查看自己的专属格子"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        # 使用代理管理器（黑名单用户看隔离池数据）
        _, backpack_mgr, _ = self._get_managers_for_user(user_id)
        
        items = backpack_mgr.get_user_items(user_id)
        slots = f"{backpack_mgr.get_user_item_count(user_id)}/{backpack_mgr.max_user_slots}"
        
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

    # ------------------- 小金库笔记命令（仅管理员可用） -------------------

    @filter.command("追加笔记")
    async def append_note(self, event: AstrMessageEvent, *, content: str = ""):
        """(管理员) 追加内容到小金库笔记"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作笔记"))
            return
        
        if not content.strip():
            yield event.plain_result("请输入要追加的内容，例如：追加笔记 记得还小明5块钱")
            return
        
        max_entries = self.config.get("max_note_entries", 5)
        self.manager.add_note(content.strip(), max_entries)
        current_note = self.manager.get_note()
        yield event.plain_result(f"📝 笔记已追加，当前完整笔记：\n{current_note}")

    @filter.command("查看笔记")
    async def view_note(self, event: AstrMessageEvent):
        """(管理员) 查看小金库笔记"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "这是贝塔的私密笔记，只有奥卢斯大人能看"))
            return
        
        note = self.manager.get_note()
        if note:
            yield event.plain_result(f"📝 小金库笔记：\n{note}")
        else:
            yield event.plain_result("📝 小金库笔记为空")

    @filter.command("删除笔记")
    async def delete_note(self, event: AstrMessageEvent, index: str = ""):
        """(管理员) 删除指定序号的笔记"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作笔记"))
            return
        
        if not index.strip():
            yield event.plain_result("请指定要删除的笔记序号，例如：删除笔记 1")
            return
        
        try:
            note_index = int(index.strip())
            if note_index <= 0:
                yield event.plain_result("错误：序号必须是正整数")
                return
        except ValueError:
            yield event.plain_result("错误：请输入有效的序号数字")
            return
        
        notes = self.manager.get_notes()
        if not notes:
            yield event.plain_result("📝 当前没有笔记可删除")
            return
        
        if note_index > len(notes):
            yield event.plain_result(f"错误：序号超出范围，当前共有 {len(notes)} 条笔记")
            return
        
        deleted_content = notes[note_index - 1]
        if self.manager.delete_note(note_index):
            current_note = self.manager.get_note()
            if current_note:
                yield event.plain_result(f"📝 已删除第 {note_index} 条笔记：{deleted_content}\n\n当前笔记：\n{current_note}")
            else:
                yield event.plain_result(f"📝 已删除第 {note_index} 条笔记：{deleted_content}\n\n笔记已清空")
        else:
            yield event.plain_result("删除失败，请检查序号是否正确")

    @filter.command("清空笔记")
    async def clear_note(self, event: AstrMessageEvent):
        """(管理员) 清空小金库笔记"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能清空笔记"))
            return
        
        self.manager.clear_note()
        yield event.plain_result("📝 小金库笔记已全部清空")

    # ------------------- 用户隔离池（黑名单）命令 -------------------

    @filter.command("零花钱拉黑")
    async def add_to_blacklist(self, event: AstrMessageEvent, user_id: str):
        """(管理员) 将用户加入黑名单，其操作将进入隔离池"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作"))
            return
        
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("请指定用户QQ号，例如：零花钱拉黑 123456789")
            return
        
        if self.isolation_manager.add_to_blacklist(user_id):
            # 触发隔离管理器创建（会自动复制当前真实数据）
            real_balance = self.manager.get_balance()
            yield event.plain_result(
                f"🚫 用户 {user_id} 已加入隔离池\n"
                f"该用户的所有账本操作将进入隔离池，不影响真实数据\n"
                f"隔离池初始余额：{real_balance}元"
            )
        else:
            yield event.plain_result(f"用户 {user_id} 已在黑名单中")

    @filter.command("零花钱解除拉黑")
    async def remove_from_blacklist(self, event: AstrMessageEvent, user_id: str):
        """(管理员) 将用户从黑名单移除"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作"))
            return
        
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("请指定用户QQ号，例如：零花钱解除拉黑 123456789")
            return
        
        if self.isolation_manager.remove_from_blacklist(user_id):
            yield event.plain_result(f"✅ 用户 {user_id} 已从黑名单移除，隔离数据已清除")
        else:
            yield event.plain_result(f"用户 {user_id} 不在黑名单中")

    @filter.command("零花钱黑名单")
    async def view_blacklist(self, event: AstrMessageEvent):
        """(管理员) 查看黑名单列表"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能查看"))
            return
        
        blacklist = self.isolation_manager.get_blacklist()
        
        if not blacklist:
            yield event.plain_result("🚫 黑名单为空")
            return
        
        response = f"🚫 黑名单用户（{len(blacklist)}人）：\n\n"
        for uid in blacklist:
            # 获取隔离管理器来读取数据
            managers = self.isolation_manager.get_isolated_managers(uid, self.manager, self.backpack_manager)
            balance = managers["money"].get_balance()
            records_count = len(managers["money"].get_all_records())
            response += f"- {uid}（隔离池余额: {balance}元，操作记录: {records_count}条）\n"
        
        response += "\n这些用户的账本操作将进入隔离池，不影响真实数据"
        yield event.plain_result(response)

    @filter.command("零花钱隔离池")
    async def view_isolation_data(self, event: AstrMessageEvent, user_id: str):
        """(管理员) 查看指定用户的隔离池数据"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能查看"))
            return
        
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("请指定用户QQ号，例如：查看隔离池 123456789")
            return
        
        if not self.isolation_manager.is_blacklisted(user_id):
            yield event.plain_result(f"用户 {user_id} 不在黑名单中")
            return
        
        # 获取隔离管理器
        managers = self.isolation_manager.get_isolated_managers(user_id, self.manager, self.backpack_manager)
        money_mgr = managers["money"]
        backpack_mgr = managers["backpack"]
        
        balance = money_mgr.get_balance()
        records = money_mgr.get_all_records()
        shared_items = backpack_mgr.get_shared_items()
        user_items = backpack_mgr.get_user_items(user_id)
        
        response = f"🔒 用户 {user_id} 的隔离池数据：\n\n"
        response += f"💰 隔离池余额：{balance}元\n\n"
        
        # 最近记录
        response += f"📋 最近操作记录（{len(records)}条）：\n"
        if records:
            for r in records[-5:]:
                type_str = "+" if r["type"] == "income" else "-"
                response += f"  {r['time']}: {type_str}{r['amount']}元 ({r['reason']})\n"
        else:
            response += "  暂无记录\n"
        
        # 背包物品
        response += f"\n🎒 隔离池背包（{len(shared_items)}件）：\n"
        if shared_items:
            for item in shared_items:
                response += f"  - {item['name']}\n"
        else:
            response += "  空\n"
        
        response += f"\n🎁 隔离池礼物（{len(user_items)}件）：\n"
        if user_items:
            for item in user_items:
                response += f"  - {item['name']} (来自{item.get('from', '未知')})\n"
        else:
            response += "  空\n"
        
        yield event.plain_result(response)

    # ------------------- 存折命令 -------------------

    @filter.command("存入存折")
    async def deposit_to_savings(self, event: AstrMessageEvent, amount: str, *, reason: str = "存入存折"):
        """(管理员) 从小金库转入存折"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作存折"))
            return

        try:
            amount_value = float(amount)
            if amount_value <= 0:
                yield event.plain_result("错误：金额必须是正数。")
                return
        except ValueError:
            yield event.plain_result("错误：金额格式不正确。")
            return

        # 检查小金库余额
        current_balance = self.manager.get_balance()
        if amount_value > current_balance:
            yield event.plain_result(f"错误：小金库余额不足。当前小金库余额：{current_balance}元")
            return

        operator_id = event.get_sender_id()
        
        # 从小金库扣款
        if not self.manager.add_expense(amount_value, f"转入存折：{reason}", operator_id):
            yield event.plain_result("从小金库扣款失败。")
            return
        
        # 存入存折
        if not self.manager.deposit_to_savings(amount_value, reason, operator_id):
            # 回滚小金库扣款
            self.manager.add_income(amount_value, "存折存入失败回滚", operator_id)
            yield event.plain_result("存入存折失败。")
            return
        
        new_pocket_balance = self.manager.get_balance()
        new_savings_balance = self.manager.get_savings_balance()
        
        yield event.plain_result(
            f"📒 存折存入成功！\n"
            f"💰 存入金额：{amount_value}元\n"
            f"📝 原因：{reason}\n"
            f"💳 小金库余额：{new_pocket_balance}元\n"
            f"📒 存折余额：{new_savings_balance}元"
        )

    @filter.command("查看存折")
    async def view_savings(self, event: AstrMessageEvent, num: str = "5"):
        """(管理员) 查看存折余额和最近记录"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能查看存折"))
            return

        try:
            count = int(num)
            if count <= 0:
                count = 5
        except ValueError:
            count = 5

        balance = self.manager.get_savings_balance()
        pending_count = len(self.manager.get_pending_withdrawals())
        
        response = f"📒 存折余额：{balance}元\n"
        if pending_count > 0:
            response += f"⏳ 待审批申请：{pending_count}个\n"
        response += "\n💡 使用「待审批列表」查看详细申请"
        
        yield event.plain_result(response)

    @filter.command("批准取款")
    async def approve_withdrawal(self, event: AstrMessageEvent, application_id: str):
        """(管理员) 批准存折取款申请"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能审批取款"))
            return

        if not application_id.strip():
            yield event.plain_result("请指定申请ID，例如：批准取款 1234567890123")
            return

        operator_id = event.get_sender_id()
        success, amount, reason, source_info = self.manager.approve_withdrawal(application_id.strip(), operator_id)
        
        if not success:
            yield event.plain_result(f"批准失败：{reason}")
            return
        
        # approve_withdrawal 内部已自动转入小金库
        new_pocket_balance = self.manager.get_balance()
        new_savings_balance = self.manager.get_savings_balance()
        
        # 向原窗口发送审批结果通知
        if source_info:
            notify_msg = (
                f"✅ 存折取款申请已批准！\n"
                f"💰 取款金额：{amount}元\n"
                f"📝 原因：{reason}\n"
                f"💳 小金库余额：{new_pocket_balance}元\n"
                f"📒 存折余额：{new_savings_balance}元"
            )
            try:
                if source_info.get("is_group") and source_info.get("group_id"):
                    await event.bot.send_group_msg(group_id=int(source_info["group_id"]), message=notify_msg)
                elif source_info.get("user_id"):
                    await event.bot.send_private_msg(user_id=int(source_info["user_id"]), message=notify_msg)
            except Exception as e:
                logger.warning(f"[PocketMoney] 发送审批结果通知失败: {e}")
        
        yield event.plain_result(
            f"✅ 取款申请已批准！\n"
            f"📋 申请ID：{application_id}\n"
            f"💰 取款金额：{amount}元\n"
            f"📝 原因：{reason}\n"
            f"💳 小金库余额：{new_pocket_balance}元\n"
            f"📒 存折余额：{new_savings_balance}元"
        )

    @filter.command("拒绝取款")
    async def reject_withdrawal(self, event: AstrMessageEvent, application_id: str, *, reject_reason: str = ""):
        """(管理员) 拒绝存折取款申请"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能审批取款"))
            return

        if not application_id.strip():
            yield event.plain_result("请指定申请ID，例如：拒绝取款 1234567890123 不批准的原因")
            return

        operator_id = event.get_sender_id()
        success, amount, reason, source_info = self.manager.reject_withdrawal(
            application_id.strip(), reject_reason.strip(), operator_id
        )
        
        if not success:
            yield event.plain_result(f"拒绝失败：{reason}")
            return
        
        reject_msg = f"（{reject_reason}）" if reject_reason.strip() else ""
        savings_balance = self.manager.get_savings_balance()
        
        # 向原窗口发送审批结果通知
        if source_info:
            notify_msg = (
                f"❌ 存折取款申请被拒绝{reject_msg}\n"
                f"💰 申请金额：{amount}元\n"
                f"📝 申请原因：{reason}\n"
                f"📒 存折余额：{savings_balance}元"
            )
            try:
                if source_info.get("is_group") and source_info.get("group_id"):
                    await event.bot.send_group_msg(group_id=int(source_info["group_id"]), message=notify_msg)
                elif source_info.get("user_id"):
                    await event.bot.send_private_msg(user_id=int(source_info["user_id"]), message=notify_msg)
            except Exception as e:
                logger.warning(f"[PocketMoney] 发送审批结果通知失败: {e}")
        
        yield event.plain_result(
            f"❌ 取款申请已拒绝{reject_msg}\n"
            f"📋 申请ID：{application_id}\n"
            f"💰 申请金额：{amount}元\n"
            f"📝 申请原因：{reason}\n"
            f"📒 存折余额：{savings_balance}元"
        )

    @filter.command("直接取款")
    async def direct_withdrawal(self, event: AstrMessageEvent, amount: str, *, reason: str = "管理员直接取款"):
        """(管理员) 直接从存折取款到小金库"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能操作存折"))
            return

        try:
            amount_value = float(amount)
            if amount_value <= 0:
                yield event.plain_result("错误：金额必须是正数。")
                return
        except ValueError:
            yield event.plain_result("错误：金额格式不正确。")
            return

        # 检查存折余额
        savings_balance = self.manager.get_savings_balance()
        if amount_value > savings_balance:
            yield event.plain_result(f"错误：存折余额不足。当前存折余额：{savings_balance}元")
            return

        operator_id = event.get_sender_id()
        
        # 从存折取款（withdraw_from_savings 内部已自动转入小金库）
        if not self.manager.withdraw_from_savings(amount_value, reason, operator_id):
            yield event.plain_result("从存折取款失败。")
            return
        
        new_pocket_balance = self.manager.get_balance()
        new_savings_balance = self.manager.get_savings_balance()
        
        yield event.plain_result(
            f"📒 存折取款成功！\n"
            f"💰 取款金额：{amount_value}元\n"
            f"📝 原因：{reason}\n"
            f"💳 小金库余额：{new_pocket_balance}元\n"
            f"📒 存折余额：{new_savings_balance}元"
        )

    @filter.command("待审批取款")
    async def pending_withdrawals(self, event: AstrMessageEvent):
        """(管理员) 查看所有待审批的取款申请"""
        if not self._is_admin(event):
            yield event.plain_result(self.config.get("admin_permission_denied_msg", 
                "只有奥卢斯大人能查看"))
            return

        pending = self.manager.get_pending_withdrawals()
        
        if not pending:
            yield event.plain_result("📋 当前没有待审批的取款申请")
            return
        
        response = f"📋 待审批取款申请（{len(pending)}个）：\n\n"
        for w in pending:
            response += (
                f"📌 申请ID：{w['id']}\n"
                f"   金额：{w['amount']}元\n"
                f"   原因：{w['reason']}\n"
                f"   时间：{w['time']}\n\n"
            )
        
        response += f"📒 存折余额：{self.manager.get_savings_balance()}元\n\n"
        response += "回复「批准取款 <ID>」批准\n回复「拒绝取款 <ID> <原因>」拒绝"
        
        yield event.plain_result(response)

    async def terminate(self):
        """插件终止时保存数据"""
        self.manager._save_data()
        self.thank_manager._save_data()
        self.backpack_manager._save_data()
        self.isolation_manager._save_data()
