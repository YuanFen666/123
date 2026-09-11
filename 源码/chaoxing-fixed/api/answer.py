import configparser
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from re import sub
from typing import Optional

import httpx
import requests
from openai import OpenAI
from urllib3 import disable_warnings, exceptions

from api.answer_check import *
from api.logger import logger

# 关闭警告
disable_warnings(exceptions.InsecureRequestWarning)

__all__ = ["CacheDAO", "Tiku", "TikuYanxi", "TikuLike", "TikuAdapter", "TikuAnevol", "TikuIcodef",
           "TikuChain", "AI", "SiliconFlow"]

class CacheDAO:
    """
    @Author: SocialSisterYi
    @Reference: https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy
    """
    DEFAULT_CACHE_FILE = "cache.json"

    def __init__(self, file: str = DEFAULT_CACHE_FILE):
        self.cache_file = Path(file)
        self._lock = threading.RLock()
        if not self.cache_file.is_file():
            self._write_cache({})

    def _read_cache(self) -> dict:
        # 新增缓存文件读取的异常处理
        try:
            with self._lock:
                if not self.cache_file.is_file():
                    return {}
                try:
                    with self.cache_file.open("r", encoding="utf8") as fp:
                        return json.load(fp)
                except json.JSONDecodeError as e:
                    logger.error(f"缓存文件 JSON 解析失败: {e}, 尝试恢复...")
                    # 尝试从原始二进制中以 utf-8 忽略错误地恢复有效 JSON 段
                    try:
                        raw = self.cache_file.read_bytes()
                        text = raw.decode("utf-8", errors="ignore")
                        start = text.find('{')
                        end = text.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            try:
                                return json.loads(text[start:end+1])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # 若无法恢复，备份损坏文件并返回空缓存
                    try:
                        bak_name = f"{self.cache_file.name}.bak.{int(time.time())}"
                        bak_path = self.cache_file.with_name(bak_name)
                        shutil.copy2(self.cache_file, bak_path)
                        logger.error(f"缓存文件已损坏，已备份为: {bak_path}，将使用空缓存继续运行")
                    except Exception as ex:
                        logger.error(f"备份损坏缓存失败: {ex}")
                    return {}
                except UnicodeDecodeError as e:
                    logger.error(f"缓存文件编码读取失败: {e}, 采用恢复策略...")
                    try:
                        raw = self.cache_file.read_bytes()
                        text = raw.decode("utf-8", errors="ignore")
                        start = text.find('{')
                        end = text.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            try:
                                return json.loads(text[start:end+1])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        bak_name = f"{self.cache_file.name}.bak.{int(time.time())}"
                        bak_path = self.cache_file.with_name(bak_name)
                        shutil.copy2(self.cache_file, bak_path)
                        logger.error(f"缓存文件编码错误，已备份为: {bak_path}，将使用空缓存继续运行")
                    except Exception as ex:
                        logger.error(f"备份损坏缓存失败: {ex}")
                    return {}
        except Exception as e:
            logger.error(f"读取缓存异常: {e}")
            return {}

    def _write_cache(self, data: dict) -> None:
        # 为缓存写入加锁，防止并发写入损坏文件
        try:
            with self._lock:
                parent = self.cache_file.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                # 写入临时文件后原子替换，减少并发写入时的损坏风险
                fd, tmp_path = tempfile.mkstemp(prefix=self.cache_file.name, dir=str(parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf8") as fp:
                        json.dump(data, fp, ensure_ascii=False, indent=4)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(tmp_path, str(self.cache_file))
                except Exception as e:
                    # 清理临时文件
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                    logger.error(f"Failed to write cache atomically: {e}")
        except IOError as e:
            logger.error(f"Failed to write cache: {e}")

    def get_cache(self, question: str) -> Optional[str]:
        data = self._read_cache()
        return data.get(question)

    def add_cache(self, question: str, answer: str) -> None:
        # 为缓存写入加锁，防止并发写入损坏文件
        with self._lock:
            data = self._read_cache()
            data[question] = answer
            self._write_cache(data)


# TODO: 重构此部分代码，将此类改为抽象类，加载题库方法改为静态方法，禁止直接初始化此类
class Tiku:
    CONFIG_PATH = os.path.join(os.getcwd(), "config.ini")  # TODO: 从运行参数中获取config路径
    DISABLE = False     # 停用标志
    SUBMIT = False      # 提交标志
    COVER_RATE = 0.8    # 覆盖率
    true_list = []
    false_list = []
    def __init__(self) -> None:
        self._name = None
        self._api = None
        self._conf = None

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def api(self):
        return self._api

    @api.setter
    def api(self, value):
        self._api = value

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self,value):
        self._token = value

    def init_tiku(self):
        # 仅用于题库初始化, 应该在题库载入后作初始化调用, 随后才可以使用题库
        # 尝试根据配置文件设置提交模式
        if not self._conf:
            self.config_set(self._get_conf())
        if not self.DISABLE:
            # 设置提交模式
            self.SUBMIT = True if self._conf['submit'] == 'true' else False
            self.COVER_RATE = float(self._conf['cover_rate'])
            self.true_list = self._conf['true_list'].split(',')
            self.false_list = self._conf['false_list'].split(',')
            # 调用自定义题库初始化
            self._init_tiku()

    def _init_tiku(self):
        # 仅用于题库初始化, 例如配置token, 交由自定义题库完成
        pass

    def config_set(self,config):
        self._conf = config

    def _get_conf(self):
        """
        从默认配置文件查询配置, 如果未能查到, 停用题库
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.CONFIG_PATH, encoding="utf8")
            return config['tiku']
        except (KeyError, FileNotFoundError):
            logger.info("未找到tiku配置, 已忽略题库功能")
            self.DISABLE = True
            return None
    def query(self,q_info:dict) -> Optional[str]:
        if self.DISABLE:
            return None

        # 预处理, 去除【单选题】这样与标题无关的字段
        logger.debug(f"原始标题：{q_info['title']}")
        # 去题号："1. xxx" / "12、xxx" / "3) xxx"。
        # 原版是 ^\d+ ，会把 "1+1等于几？" 削成 "+1等于几？"、"1921年..." 削成 "年..."，
        # 题干直接变形导致搜不到。这里要求「数字后面必须跟标点」且数字不超过 3 位，
        # 既保留去题号的能力，又不会吃掉题干里的年份和算式。
        q_info['title'] = sub(r'^\s*\d{1,3}\s*[.、,，)）:：]\s*', '', q_info['title'])
        q_info['title'] = sub(r'（\d+\.\d+分）$', '', q_info['title'])
        logger.debug(f"处理后标题：{q_info['title']}")

        # 先过缓存
        cache_dao = CacheDAO()
        answer = cache_dao.get_cache(q_info['title'])
        if answer:
            logger.info(f"从缓存中获取答案：{q_info['title']} -> {answer}")
            return answer.strip()
        else:
            answer = self._query(q_info)
            if answer:
                answer = answer.strip()
                cache_dao.add_cache(q_info['title'], answer)
                logger.info(f"从{self.name}获取答案：{q_info['title']} -> {answer}")
                if check_answer(answer, q_info['type'], self):
                    return answer
                else:
                    logger.info(f"从{self.name}获取到的答案类型与题目类型不符，已舍弃")
                    return None

            # 注意：这里只是「这个题库没给出答案」，不等于调用失败——
            # 题不在题库里、题库服务故障、熔断中，都会走到这里。真正的失败原因
            # 已经由各题库在 _ask 里按级别记过了，这里再报一次 ERROR 只会让人误以为
            # 整条链挂了（实际上后面还有题库/AI 兜底，最后往往答得好好的）。
            if getattr(self, "_in_chain", False):
                # 题库链的中间环节：没查到就继续问下一个，不吵人
                logger.debug(f"{self.name}未命中，继续问下一个题库：{q_info['title']}")
            elif getattr(self, "_is_chain", False):
                # 整条链都交白卷了，这才是真正需要用户看到的一行
                logger.error(f"题库链全部未命中（没查到答案），该题将走随机作答：{q_info['title']}")
            else:
                logger.info(f"从{self.name}未获取到答案（该题将走随机作答）：{q_info['title']}")
        return None



    def _query(self,q_info:dict) -> Optional[str]:
        """
        查询接口, 交由自定义题库实现
        """
        pass


    def get_tiku_from_config(self):
        """
        从配置文件加载题库, 这个配置可以是用户提供, 可以是默认配置文件
        """
        if not self._conf:
            # 尝试从默认配置文件加载
            self.config_set(self._get_conf())
        if self.DISABLE:
            return self
        try:
            cls_name = self._conf['provider']
            if not cls_name:
                raise KeyError
        except KeyError:
            self.DISABLE = True
            logger.error("未找到题库配置, 已忽略题库功能")
            return self
        # 多题库顺序回退：provider = TikuIcodef,TikuAnevol
        # 按配置里的先后顺序查询，第一个给出有效答案的题库胜出，后面的不再请求。
        if re.search(r"[,;，、\s]", cls_name.strip()):
            chain = TikuChain()
            chain.config_set(self._conf)
            return chain
        # FIXME: Implement using StrEnum instead. This is not only buggy but also not safe
        new_cls = globals()[cls_name]()
        new_cls.config_set(self._conf)
        return new_cls

    def judgement_select(self, answer: str) -> bool:
        """
        这是一个专用的方法, 要求配置维护两个选项列表, 一份用于正确选项, 一份用于错误选项, 以应对题库对判断题答案响应的各种可能的情况
        它的作用是将获取到的答案answer与可能的选项列对比并返回对应的布尔值
        """
        if self.DISABLE:
            return False
        # 对响应的答案作处理
        answer = answer.strip()
        if answer in self.true_list:
            return True
        elif answer in self.false_list:
            return False
        else:
            # 无法判断, 随机选择
            logger.error(f'无法判断答案 -> {answer} 对应的是正确还是错误, 请自行判断并加入配置文件重启脚本, 本次将会随机选择选项')
            return random.choice([True,False])

    def get_submit_params(self):
        """
        这是一个专用方法, 用于根据当前设置的提交模式, 响应对应的答题提交API中的pyFlag值
        """
        # 留空直接提交, 1保存但不提交
        if self.SUBMIT:
            return ""
        else:
            return "1"


# ---------------------------------------------------------------------------
# 答案归一化公共逻辑（TikuAnevol / TikuIcodef / AI 共用）
# ---------------------------------------------------------------------------
# 各题库返回的答案形式不一样（ANEVOL 给「选项字母」，网课小工具给「选项原文」，
# 大模型给「选项原文或字母」），但交回 chaoxing 之前要处理的坑是一样的
# （见各方法注释），所以抽出来共用。这个混入类必须定义在 Tiku 之后、
# 所有使用者之前。
class _AnswerNormalizeMixin:
    @staticmethod
    def _clean_option(text) -> str:
        """去掉选项前面的 A. / A、 / A． 之类标号。"""
        if text is None:
            return ""
        s = re.sub(r"^[A-Za-z]\s*[\.、．，,：:）\)]\s*", "", str(text).strip())
        return s.strip()

    @staticmethod
    def _split_options(q_info: dict) -> list:
        """把 chaoxing 传来的 "A. 甲\\nB. 乙" 拆成 ["甲", "乙"]（已去掉标号）。"""
        out = []
        for line in (q_info.get("options") or "").split("\n"):
            opt = _AnswerNormalizeMixin._clean_option(line)
            if opt:
                out.append(opt)
        return out

    @staticmethod
    def _parse_letters(answer, option_count):
        """从题库返回里解析出选项下标列表；不是字母形式则返回 None。"""
        if not answer:
            return None
        s = answer.strip()
        # 全角字母转半角
        s = "".join(chr(ord(c) - 0xFEE0) if 0xFF21 <= ord(c) <= 0xFF3A else c for c in s)
        s = re.sub(r"[正、确错误答案选项,，。;；|/\\\s\[\]\"'`]+", "", s)

        # 形式一：纯字母串 "A" / "ABD"
        if s and re.fullmatch(r"[A-Za-z]+", s):
            cand = [_ANEVOL_LETTERS.index(c.upper()) for c in s]
            if all(0 <= i < option_count for i in cand):
                return sorted(set(cand))
            return None

        # 形式二："A和C" / "1、3"
        cand = []
        for t in re.findall(r"[A-Za-z]|\d+", answer):
            i = int(t) - 1 if t.isdigit() else _ANEVOL_LETTERS.index(t.upper())
            if 0 <= i < option_count:
                cand.append(i)
        return sorted(set(cand)) if cand else None

    @staticmethod
    def _normalize_judgement(answer, options) -> str:
        """把各种写法的判断答案归一化成 '正确' / '错误'（必须落在 true_list/false_list 里）。"""
        t = (answer or "").strip()
        if t in _ANEVOL_TRUE_WORDS:
            return "正确"
        if t in _ANEVOL_FALSE_WORDS:
            return "错误"
        # 先判否再判是（"不正确" 里含 "正确"）
        for w in ("错误", "不正确", "不对", "错", "×", "✗"):
            if w in t:
                return "错误"
        if t in ("x", "X"):
            return "错误"
        for w in ("正确", "√", "✓"):
            if w in t:
                return "正确"
        if t in ("对", "是") or t.startswith("对"):
            return "正确"
        # 可能返回的是选项字母，用选项原文反推
        if options:
            idx = _AnswerNormalizeMixin._parse_letters(t, len(options))
            if idx:
                content = options[idx[0]]
                for w in ("错误", "不正确", "不对", "错", "否"):
                    if w in content:
                        return "错误"
                for w in ("正确", "对", "√", "是"):
                    if w in content:
                        return "正确"
        return t

    def _safe_single(self, text) -> str:
        """单选答案净化：既要能被子序列匹配到正确选项，又要过 check_single()。"""
        t = "".join(ch for ch in str(text or "").strip() if ch not in _ANEVOL_CUT_CHARS)
        if t and (t in self.true_list or t in self.false_list) and len(t) > 1:
            # 恰好等于判断词的选项会被 check_answer 当成类型不符丢掉，
            # 少一个字符仍是原子序列，但能绕开这个判定。
            t = t[:-1]
        return t

    def _normalize_text_answer(self, answer, qtype, options):
        """
        题库直接返回「明文答案」时的归一化（网课小工具/icodef 这类）。

        实测格式：单选给选项原文，多选给 '#' 连接的多个答案（也可能是换行），
        判断题给 "正确/错误/√/×"。
        """
        text = str(answer or "").strip()
        if not text:
            return None

        # 多选答案用 '#' 或换行连接，先拆开
        parts = [p.strip() for p in re.split(r"[#\n]+", text) if p.strip()]
        if not parts:
            parts = [text]

        # 有些题库也会返回 "AB" / "A,C" 这种字母形式。字母形式里不会出现中文，
        # 用它做闸门，免得把 "COVID-19" 这类明文答案误当成选项字母。
        letters = None
        if options and not re.search(r"[\u4e00-\u9fff]", text):
            letters = self._parse_letters(text, len(options))

        if qtype == "multiple":
            if letters:
                return "\n".join(self._safe_single(options[i]) for i in letters)
            picked = [self._safe_single(p) for p in parts]
            picked = [p for p in picked if p]
            return "\n".join(picked) if picked else None

        if qtype == "single":
            if letters:
                return self._safe_single(options[letters[0]])
            return self._safe_single(parts[0])

        if qtype == "judgement":
            return self._normalize_judgement(text, options)

        return text


# 按照以下模板实现更多题库

class TikuYanxi(Tiku):
    # 言溪题库实现
    def __init__(self) -> None:
        super().__init__()
        self.name = '言溪题库'
        self.api = 'https://tk.enncy.cn/query'
        self._token = None
        self._token_index = 0   # token队列计数器
        self._times = 100   # 查询次数剩余, 初始化为100, 查询后校对修正

    def _query(self,q_info:dict):
        res = requests.get(
            self.api,
            params={
                'question':q_info['title'],
                'token': self._token,
                # 'type':q_info['type'], #修复478题目类型与答案类型不符（不想写后处理了）
                # 没用，就算有type和options，言溪题库还是可能返回类型不符，问了客服，type仅用于收集
            },
            verify=False
        )
        if res.status_code == 200:
            res_json = res.json()
            if not res_json['code']:
                # 如果是因为TOKEN次数到期, 则更换token
                if self._times == 0 or '次数不足' in res_json['data']['answer']:
                    logger.info(f'TOKEN查询次数不足, 将会更换并重新搜题')
                    self._token_index += 1
                    self.load_token()
                    # 重新查询
                    return self._query(q_info)
                logger.error(f'{self.name}查询失败:\n\t剩余查询数{res_json["data"].get("times",f"{self._times}(仅参考)")}:\n\t消息:{res_json["message"]}')
                return None
            self._times = res_json["data"].get("times",self._times)
            return res_json['data']['answer'].strip()
        else:
            logger.error(f'{self.name}查询失败:\n{res.text}')
        return None

    def load_token(self):
        token_list = self._conf['tokens'].split(',')
        if self._token_index == len(token_list):
            # TOKEN 用完
            logger.error('TOKEN用完, 请自行更换再重启脚本')
            raise PermissionError(f'{self.name} TOKEN 已用完, 请更换')
        self._token = token_list[self._token_index]

    def _init_tiku(self):
        self.load_token()

class TikuLike(Tiku):
    # Like知识库实现
    def __init__(self) -> None:
        super().__init__()
        self.name = 'Like知识库'
        self.ver = '1.0.8' #对应官网API版本
        self.query_api = 'https://api.datam.site/search'
        self.balance_api = 'https://api.datam.site/balance'
        self.homepage = 'https://www.datam.site'
        self._model = None
        self._token = None
        self._times = -1
        self._search = False
        self._count = 0

    def _query(self,q_info:dict):
        q_info_map = {"single":"【单选题】","multiple":"【多选题】","completion":"【填空题】","judgement":"【判断题】"}
        api_params_map = {0:"others",1:"choose",2:"fills",3:"judge"}
        q_info_prefix = q_info_map.get(q_info['type'],"【其他类型题目】")
        option_map = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5, "G": 6, "H": 7, 'a': 0, "b": 1, "c": 2, "d": 3,
                      "e": 4, "f": 5, "g": 6, "h": 7}
        options = ', '.join(q_info['options']) if isinstance(q_info['options'], list) else q_info['options']
        question = f"{q_info_prefix}{q_info['title']}\n{options}"
        ret = ""
        ans = ""
        res = requests.post(
            self.query_api,
            json={
                'query': question,
                'token': self._token,
                'model': self._model if self._model else '',
                'search': self._search
            },
            verify=False
        )

        if res.status_code == 200:
            res_json = res.json()
            q_type = res_json['data'].get('type', 0)
            params = api_params_map.get(q_type, "")
            tans = res_json['data'].get(params, "")
            ans = ""
            match q_type:
                case 1:
                    for i in tans:
                        ans = ans + q_info['options'][option_map[i]] + '\n'
                case 2:
                    for i in tans:
                        ans = ans + i + '\n'
                case 3:
                    ans = "正确" if tans == 1 else "错误"
                case 0:
                    ans = tans
        else:
            logger.error(f'{self.name}查询失败:\n{res.text}')
            return None

        ret += str(ans)

        self._times -= 1

        #10次查询后更新实际次数
        self._count = (self._count+1) % 10

        if self._count == 0:
            self.update_times()

        return ret

    def update_times(self):
        res = requests.post(
            self.balance_api,
            json={
                'token': self._token,
            },
            verify=False
        )
        if res.status_code == 200:
            res_json = res.json()
            self._times = res_json["data"].get("balance",self._times)
            logger.info(f"当前LIKE知识库Token剩余查询次数为: {self._times}")
        else:
            logger.error('TOKEN出现错误，请检查后再试')

    def load_token(self):
        token = self._conf['tokens'].split(',')[-1] if ',' in self._conf['tokens'] else self._conf['tokens']
        self._token = token

    def load_config(self):
        self._search = self._conf['likeapi_search']
        self._model = self._conf['likeapi_model']
        var_params = {"likeapi_search": self._search, "likeapi_model": self._model}
        config_params = {"likeapi_search": False, "likeapi_model": None}

        for k,v in config_params.items():
            if k in self._conf:
                var_params[k] = self._conf[k]
            else:
                var_params[k] = v

    def _init_tiku(self):
        self.load_token()
        self.load_config()
        self.update_times()

class TikuAdapter(Tiku):
    # TikuAdapter题库实现 https://github.com/DokiDoki1103/tikuAdapter
    def __init__(self) -> None:
        super().__init__()
        self.name = 'TikuAdapter题库'
        self.api = ''

    def _query(self, q_info: dict):
        # 判断题目类型
        if q_info['type'] == "single":
            type = 0
        elif q_info['type'] == 'multiple':
            type = 1
        elif q_info['type'] == 'completion':
            type = 2
        elif q_info['type'] == 'judgement':
            type = 3
        else:
            type = 4

        options = q_info['options']
        res = requests.post(
            self.api,
            json={
                'question': q_info['title'],
                'options': [sub(r'^[A-Za-z]\.?、?\s?', '', option) for option in options.split('\n')],
                'type': type
            },
            verify=False
        )
        if res.status_code == 200:
            res_json = res.json()
            # if bool(res_json['plat']):
            # plat无论搜没搜到答案都返回0
            # 这个参数是tikuadapter用来设定自定义的平台类型
            if not len(res_json['answer']['bestAnswer']):
                logger.error("查询失败, 返回：" + res.text)
                return None
            sep = "\n"
            return sep.join(res_json['answer']['bestAnswer']).strip()
        # else:
        #   logger.error(f'{self.name}查询失败:\n{res.text}')
        return None

    def _init_tiku(self):
        # self.load_token()
        self.api = self._conf['url']

# ---------------------------------------------------------------------------
# AI 兜底（题库链的最后一根救命稻草）
# ---------------------------------------------------------------------------
# 把 AI 放在题库链最后一位时（provider = TikuIcodef,TikuAnevol,AI），
# 前面所有题库都没给出答案的题目会交给大模型来答，而不是像原来那样随机瞎填。
_AI_CB_THRESHOLD = 5        # 连续失败多少次后熔断
_AI_CB_PAUSE = 300          # 熔断暂停秒数
_AI_FATAL_PAUSE = 600       # 鉴权/余额类错误的暂停秒数（重试无意义）
_AI_FATAL_WORDS = ("insufficient", "quota", "balance", "authentication",
                   "invalid api key", "unauthorized", "余额", "额度", "欠费", "无效")


class AI(_AnswerNormalizeMixin, Tiku):
    """
    AI 大模型答题实现（任意 OpenAI 兼容接口）。

    为「兜底」这个角色补的三件事（原实现当兜底不够可靠）：
    1. 没配 key/endpoint/model 时**初始化阶段就明确报错并跳过**，
       而不是等每道题都发一次注定失败的请求、只在日志里丢一句
       「无法解析大模型输出内容」——那样根本看不出是没配 key。
    2. 请求间隔加锁（jobs=8 时多个 worker 会并发答题，不加锁等于没限流），
       并加了熔断：连续失败或余额/鉴权类错误时先停一会儿，别把整章拖死。
    3. 大模型返回的答案走和题库一样的归一化（_normalize_text_answer），
       保证「选项原文 -> 提交字母」这一步与其它题库行为一致。
    """

    # AI大模型答题实现
    def __init__(self) -> None:
        super().__init__()
        self.name = 'AI大模型答题'
        self.last_request_time = None
        self.client = None
        self.min_interval_seconds = 3.0
        self._rate_lock = threading.Lock()
        self._consec_fail = 0
        self._pause_until = 0.0
        self._cb_lock = threading.Lock()

    # ---------------- 熔断 / 限流 ----------------
    def _pause_remaining(self) -> float:
        with self._cb_lock:
            return max(0.0, self._pause_until - time.time())

    def _cb_report(self, ok: bool, fatal: bool = False):
        with self._cb_lock:
            if ok:
                self._consec_fail = 0
                return
            if fatal:
                if self._pause_until < time.time() + _AI_FATAL_PAUSE:
                    self._pause_until = time.time() + _AI_FATAL_PAUSE
                    self._consec_fail = 0
                return
            self._consec_fail += 1
            if self._consec_fail >= _AI_CB_THRESHOLD and self._pause_until < time.time():
                self._pause_until = time.time() + _AI_CB_PAUSE
                self._consec_fail = 0
                logger.warning("AI 兜底连续 {} 次失败，暂停 {} 秒不再调用".format(
                    _AI_CB_THRESHOLD, _AI_CB_PAUSE))

    def _wait_interval(self):
        # 多个 worker 会并发答题，间隔判断必须加锁，否则等于没有间隔
        with self._rate_lock:
            wait = self.min_interval_seconds - (time.time() - (self.last_request_time or 0.0))
            if wait > 0:
                logger.debug(f"API请求间隔过短, 等待 {wait:.1f} 秒")
                time.sleep(wait)
            self.last_request_time = time.time()

    def _chat(self, model, messages):
        """所有模型调用都走这里：统一限流 + 熔断 + 错误分类。失败返回 None。"""
        self._wait_interval()
        try:
            return self.client.chat.completions.create(model=model, messages=messages)
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status_code", None)
            text = "{}: {}".format(type(e).__name__, str(e)[:200])
            fatal = status in (401, 402, 403) or any(w in str(e).lower() for w in _AI_FATAL_WORDS)
            self._cb_report(False, fatal=fatal)
            if fatal:
                logger.error("AI 兜底调用被拒（余额/鉴权问题）-> {}".format(text))
            else:
                logger.warning("AI 兜底调用失败 -> {}".format(text))
            return None

    def _query(self, q_info: dict):
        def remove_md_json_wrapper(md_str):
            # 使用正则表达式匹配Markdown代码块并提取内容
            pattern = r'^\s*```(?:json)?\s*(.*?)\s*```\s*$'
            match = re.search(pattern, md_str, re.DOTALL)
            return match.group(1).strip() if match else md_str.strip()

        if self.client is None:
            return None
        remaining = self._pause_remaining()
        if remaining > 0:
            logger.debug("AI 兜底熔断中，本次跳过（还剩 {:.0f} 秒）".format(remaining))
            return None

        # 去掉 "A." / "A、" 之类标号后再喂给模型，防止模型直接回字母而非内容
        options_list = self._split_options(q_info)
        options = "\n".join(options_list)
        # 判断题目类型
        if q_info['type'] == "single":
            completion = self._chat(
                model = self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "本题为单选题，你只能选择一个选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{\"Answer\": [\"答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
                    },
                    {
                        "role": "user",
                        "content": f"题目：{q_info['title']}\n选项：{options}"
                    }
                ]
            )
        elif q_info['type'] == 'multiple':
            completion = self._chat(
                model = self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "本题为多选题，你必须选择两个或以上选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{\"Answer\": [\"答案1\",\n\"答案2\",\n\"答案3\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
                    },
                    {
                        "role": "user",
                        "content": f"题目：{q_info['title']}\n选项：{options}"
                    }
                ]
            )
        elif q_info['type'] == 'completion':
            completion = self._chat(
                model = self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "本题为填空题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
                    },
                    {
                        "role": "user",
                        "content": f"题目：{q_info['title']}"
                    }
                ]
            )
        elif q_info['type'] == 'judgement':
            completion = self._chat(
                model = self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "本题为判断题，你只能回答正确或者错误，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"正确\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
                    },
                    {
                        "role": "user",
                        "content": f"题目：{q_info['title']}"
                    }
                ]
            )
        else:
            completion = self._chat(
                model = self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "本题为简答题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"这是我的答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
                    },
                    {
                        "role": "user",
                        "content": f"题目：{q_info['title']}"
                    }
                ]
            )

        if completion is None:
            # 调用失败，_chat 里已经记过日志并报过熔断了
            return None

        try:
            response = json.loads(remove_md_json_wrapper(completion.choices[0].message.content))
            answer = response['Answer']
            # 有的模型会把 Answer 写成字符串而不是列表，直接 join 会把字符串拆成单字
            if isinstance(answer, (list, tuple)):
                text = "\n".join(str(x) for x in answer)
            else:
                text = str(answer)
            text = text.strip()
        except Exception:  # noqa: BLE001
            content = ""
            try:
                content = str(completion.choices[0].message.content)[:200]
            except Exception:  # noqa: BLE001
                pass
            logger.error("无法解析大模型输出内容 -> {}".format(content))
            self._cb_report(False)
            return None

        if not text:
            self._cb_report(False)
            return None

        self._cb_report(True)
        # 走和题库一样的归一化：字母->选项原文、单选净化、判断题归一化
        return self._normalize_text_answer(text, q_info.get('type', ''), options_list)

    def _init_tiku(self):
        self.endpoint = (self._conf.get('endpoint') or '').strip()
        self.key = (self._conf.get('key') or '').strip()
        self.model = (self._conf.get('model') or '').strip()
        self.http_proxy = (self._conf.get('http_proxy') or '').strip()
        try:
            self.min_interval_seconds = float(self._conf.get('min_interval_seconds') or 3)
        except (TypeError, ValueError):
            self.min_interval_seconds = 3.0

        missing = [n for n, v in (('endpoint', self.endpoint), ('key', self.key), ('model', self.model))
                   if not v]
        if missing:
            logger.error(
                "AI 兜底未配置完整（缺少 {}），已跳过 AI 兜底。"
                "请在 config.ini 的 [tiku] 段补齐 endpoint / key / model。".format("、".join(missing)))
            self.DISABLE = True
            return

        try:
            if self.http_proxy:
                self.client = OpenAI(http_client=httpx.Client(proxy=self.http_proxy),
                                     base_url=self.endpoint, api_key=self.key)
            else:
                self.client = OpenAI(base_url=self.endpoint, api_key=self.key)
        except Exception as e:  # noqa: BLE001
            logger.error("AI 兜底创建客户端失败，已跳过 -> {}: {}".format(type(e).__name__, e))
            self.DISABLE = True
            return

        logger.info("AI 兜底已加载 -> 接口: {} , 模型: {} (最小间隔 {}s)".format(
            self.endpoint, self.model, self.min_interval_seconds))
class SiliconFlow(Tiku):
    """硅基流动大模型答题实现"""
    def __init__(self):
        super().__init__()
        self.name = '硅基流动大模型'
        self.last_request_time = None

    def _query(self, q_info: dict):
        def remove_md_json_wrapper(md_str):
            # 解析可能存在的JSON包装
            pattern = r'^\s*```(?:json)?\s*(.*?)\s*```\s*$'
            match = re.search(pattern, md_str, re.DOTALL)
            return match.group(1).strip() if match else md_str.strip()

        # 构造请求头
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # 构造系统提示词
        system_prompt = ""
        if q_info['type'] == "single":
            system_prompt = "本题为单选题，请根据题目和选项选择唯一正确答案，输出的是选项的具体内容，而不是内容前的ABCD，并以JSON格式输出：示例回答：{\"Answer\": [\"正确选项内容\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'multiple':
            system_prompt = "本题为多选题，请选择所有正确选项，输出的是选项的具体内容，而不是内容前的ABCD，以JSON格式输出：示例回答：{\"Answer\": [\"选项1\",\"选项2\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'completion':
            system_prompt = "本题为填空题，请直接给出填空内容，以JSON格式输出：示例回答：{\"Answer\": [\"答案文本\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'judgement':
            system_prompt = "本题为判断题，请回答'正确'或'错误'，以JSON格式输出：示例回答：{\"Answer\": [\"正确\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"

        # 构造请求体
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": f"题目：{q_info['title']}\n选项：{q_info['options']}"
                }
            ],
            "stream": False,

            "max_tokens": 4096,

            "temperature": 0.7,
            "top_p": 0.7,
            "response_format": {"type": "text"}
        }

        # 处理请求间隔
        if self.last_request_time:
            interval = time.time() - self.last_request_time
            if interval < self.min_interval:
                time.sleep(self.min_interval - interval)

        try:
            response = requests.post(
                self.api_endpoint,
                headers=headers,
                json=payload,
                timeout=30
            )
            self.last_request_time = time.time()

            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                parsed = json.loads(remove_md_json_wrapper(content))
                return "\n".join(parsed['Answer']).strip()
            else:
                logger.error(f"API请求失败：{response.status_code} {response.text}")
                return None

        except Exception as e:
            logger.error(f"硅基流动API异常：{e}")
            return None

    def _init_tiku(self):
        # 从配置文件读取参数
        self.api_endpoint = self._conf.get('siliconflow_endpoint', 'https://api.siliconflow.cn/v1/chat/completions')
        self.api_key = self._conf['siliconflow_key']

        self.model_name = self._conf.get('siliconflow_model', 'deepseek-ai/DeepSeek-V3')


        self.min_interval = int(self._conf.get('min_interval_seconds', 3))


# ---------------------------------------------------------------------------
# ANEVOL 题库（自建题库，https://tiku.anevol.cn）
# ---------------------------------------------------------------------------
_ANEVOL_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ANEVOL_DEFAULT_API = "https://tiku.anevol.cn/api/search"

_ANEVOL_TRUE_WORDS = {"正确", "对", "√", "是", "T", "True", "true"}
_ANEVOL_FALSE_WORDS = {"错误", "错", "×", "否", "不对", "不正确", "F", "False", "false"}

# chaoxing 内部按这些字符切分答案（见 api/answer_check.py::cut），
# 选项内容里出现任意一个都会被误判成“多段答案”，所以单选返回前要去掉。
_ANEVOL_CUT_CHARS = set(
    "\n,，|\r\t#*-_+@~/\\ .&、"
)

_ANEVOL_TYPE_MAP = {
    "single": "single",
    "multiple": "multiple",
    "completion": "fill",
    "judgement": "judgement",
}

# 这些词意味着“调用方式/套餐”出了问题，重试没有任何意义
_ANEVOL_NO_RETRY_WORDS = ("密钥", "无效", "次数不足", "余额", "额度", "不存在")

# 这些词表示“题不在题库里”，是正常业务结果而不是故障：
# 以前这种响应会走到最后的汇总行被记成 ERROR「ANEVOL 查询失败」，
# 让人误以为题库挂了（其实后面还有别的题库/AI 兜底，最后往往答得好好的）。
_ANEVOL_NOTFOUND_WORDS = ("没有找到", "未找到", "没有相关", "未收录", "未搜索到")

# 读超时（秒）。ANEVOL 正常答一道题要 20~25 秒，超过这个数基本就是它病了，
# 再等一轮纯属浪费——所以调用侧对读超时不再重试。
_ANEVOL_READ_TIMEOUT = 40

# 额度/付费类致命错误（上游 AI 返回 HTTP 402「Payment Required」等）：
# 这种情况重试一万次也没用，直接长时间熔断并提示用户去查账单。
_ANEVOL_FATAL_WORDS = (
    "payment required", "insufficient", "quota", "credit",
    "余额不足", "额度不足", "欠费", "充值", "配额不足", "余额", "额度",
)
_ANEVOL_FATAL_PAUSE = 600

# 服务级故障（HTTP != 200 或 网络异常）连续多少次后熔断。
# 注意：只数“服务故障”，不数“题不在库里”——后者是正常现象，
# 一片题目搜不到不应该把整个题库停掉。
_ANEVOL_CB_THRESHOLD = 5
_ANEVOL_CB_PAUSE_BASE = 120     # 首次熔断暂停秒数，之后翻倍
_ANEVOL_CB_PAUSE_MAX = 900      # 最长暂停 15 分钟


class TikuAnevol(_AnswerNormalizeMixin, Tiku):
    """
    ANEVOL 自建题库（tiku.anevol.cn）内置实现，不需要额外跑本地桥接服务。

    配置（config.ini 的 [tiku] 段）：
        provider = TikuAnevol
        url      = https://tiku.anevol.cn/api/search    ; 可留空，走默认值
        tokens   = 你的token                             ; 多个用英文逗号分隔，只用第一个
        anevol_min_interval = 0.3                        ; 可选，请求最小间隔(秒)

    另外支持环境变量 ANEVOL_TOKEN。

    两个必须注意的坑（都踩过）：
    1. ANEVOL 对选择题返回的是「选项字母」(如 "AB")，而 chaoxing 的 clean_res()
       会把答案开头的字母直接删掉，删成空串后 is_subsequence("", 选项) 恒为真，
       会错误地匹配到第一个选项。所以这里必须把字母还原成选项原文再返回。
    2. 选项原文里若有逗号/顿号/空格，check_single() 会判定失败（cut() 会把它
       切成多段）。因此单选返回前要剥掉这些分隔符——只删字符不会破坏后面的
       子序列匹配，却能让长度校验通过。
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = "ANEVOL题库"
        self.api = _ANEVOL_DEFAULT_API
        self._token = ""
        self.min_interval = 0.3
        self._last_call = 0.0
        self._interval_lock = threading.Lock()
        # 熔断器：ANEVOL 服务端故障时每次请求要 20 秒以上才返回，
        # 连续失败到阈值就先停一会儿，别把一整章拖到几十分钟。
        self._consec_fail = 0
        self._pause_until = 0.0
        self._pause_round = 0
        self._cb_lock = threading.Lock()

    # ---------------- 配置 ----------------
    def _init_tiku(self):
        url = (self._conf.get("anevol_url") or "").strip()
        if not url:
            cfg_url = (self._conf.get("url") or "").strip()
            # url 是共用字段：TikuAdapter 会把它写成 http://127.0.0.1:8787/，
            # 那种本地地址不能当成 ANEVOL 的地址用。
            if cfg_url and "127.0.0.1" not in cfg_url and "localhost" not in cfg_url:
                url = cfg_url
        self.api = url or _ANEVOL_DEFAULT_API

        token = (self._conf.get("anevol_token") or "").strip()
        if not token:
            tokens = (self._conf.get("tokens") or "").strip()
            if tokens:
                token = tokens.split(",")[0].strip()
        if not token:
            token = (os.environ.get("ANEVOL_TOKEN") or "").strip()
        self._token = token

        try:
            self.min_interval = float(self._conf.get("anevol_min_interval") or 0.3)
        except (TypeError, ValueError):
            self.min_interval = 0.3

        if not self._token:
            logger.error(
                "ANEVOL题库未配置token！请在 config.ini 的 [tiku] 段填写 tokens=你的token "
                "（或设置环境变量 ANEVOL_TOKEN），否则搜题会全部跳过。"
            )
        else:
            logger.info(f"ANEVOL题库已加载 -> 接口: {self.api} , token: {self._token[:8]}...")

    # ---------------- 小工具 ----------------
    # _clean_option / _split_options / _parse_letters / _normalize_judgement /
    # _safe_single / _normalize_text_answer 都在 _AnswerNormalizeMixin 里，两个题库共用。

    def _wait_interval(self):
        with self._interval_lock:
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    def _pause_remaining(self) -> float:
        with self._cb_lock:
            return max(0.0, self._pause_until - time.time())

    def _cb_report(self, ok: bool, fatal: bool = False, count: bool = True):
        """
        ok    : 本次查询是否成功
        fatal : 额度/付费类致命错误（重试无意义）
        count : 是否计入熔断。只有“服务级故障”才计；题目搜不到不算，
                否则一片题搜不到就会把整个题库停 2 分钟。
        """
        with self._cb_lock:
            if ok:
                self._consec_fail = 0
                self._pause_round = 0
                return
            if fatal:
                # 额度/欠费类错误：继续查只是白等，直接长时间停掉并明确提示
                if self._pause_until < time.time() + _ANEVOL_FATAL_PAUSE:
                    self._pause_until = time.time() + _ANEVOL_FATAL_PAUSE
                    self._consec_fail = 0
                    logger.error(
                        "ANEVOL 上游 AI 报「额度/付费」问题（HTTP 402 之类），继续查题只是白等 {} 秒。"
                        "这几道题会走随机作答，请去 ANEVOL 控制台确认套餐额度或账单。".format(
                            _ANEVOL_FATAL_PAUSE)
                    )
                return
            if not count:
                return
            self._consec_fail += 1
            if self._consec_fail >= _ANEVOL_CB_THRESHOLD and self._pause_until < time.time():
                # 熔断时长逐级翻倍（120→240→480→900），避免服务长时间挂掉时反复白等
                self._pause_round = min(self._pause_round + 1, 4)
                pause = min(_ANEVOL_CB_PAUSE_BASE * (2 ** (self._pause_round - 1)), _ANEVOL_CB_PAUSE_MAX)
                self._pause_until = time.time() + pause
                self._consec_fail = 0
                logger.warning(
                    "ANEVOL 服务端连续 {} 次不可用，暂停 {} 秒不再请求题库"
                    "（这几道题会走随机作答，覆盖率不达标不会提交；服务恢复后会自动继续）".format(
                        _ANEVOL_CB_THRESHOLD, pause)
                )

    # ---------------- 请求 ----------------
    @staticmethod
    def _reason_from_json(res_or_body):
        """
        取出错误原因。

        坑：ANEVOL 不同分支用的字段名不一样——
        业务失败(code=0)走 `msg`，而 HTTP 500 走的是 `detail`（内容形如
        “主模型和所有备用模型都失败了。最后的错误: 调用AI API时出错: ... HTTP 402”）。
        只读 `msg` 的话 500 会被当成“未知原因”，重试短路的判断就失效了。
        """
        try:
            body = res_or_body if isinstance(res_or_body, dict) else res_or_body.json()
        except Exception:
            return ""
        if not isinstance(body, dict):
            return ""
        for key in ("msg", "detail", "message", "error"):
            val = body.get(key)
            if val:
                return str(val).strip()
        return ""

    @staticmethod
    def _is_fatal_reason(text) -> bool:
        """额度/付费类错误：再重试也没有意义。"""
        t = str(text or "").lower()
        if any(w in t for w in _ANEVOL_FATAL_WORDS):
            return True
        return bool(re.search(r"\b402\b", t))

    def _ask(self, question, options, qtype):
        """调用 ANEVOL，成功返回答案字符串，失败返回 None。"""
        if not question or not self._token:
            return None

        remaining = self._pause_remaining()
        if remaining > 0:
            logger.debug("ANEVOL 熔断中，本次跳过查询（还剩 {:.0f} 秒）".format(remaining))
            return None

        payload = {"title": question}
        if options:
            payload["options"] = "\n".join(
                "{}. {}".format(_ANEVOL_LETTERS[i], c)
                for i, c in enumerate(options) if i < len(_ANEVOL_LETTERS)
            )
        if qtype:
            payload["type"] = qtype

        max_attempts = 2
        last_err = None
        fatal_any = False
        service_fail_any = False   # 只有服务级故障才计入熔断

        for attempt in range(max_attempts):
            stop = False      # 本次是否立刻终止（不再重试）
            fatal = False     # 是否属于“重试无意义”的致命错误
            service = False   # 是否属于服务级故障（HTTP 非 200 / 网络异常）
            try:
                self._wait_interval()
                res = requests.post(
                    self.api,
                    params={"token": self._token},
                    json=payload,
                    timeout=(5, _ANEVOL_READ_TIMEOUT),
                    verify=False,
                )
                if res.status_code == 200:
                    body = None
                    try:
                        body = res.json()
                    except Exception:
                        pass
                    if body is None:
                        last_err = "响应不是合法JSON: " + res.text[:120]
                        stop = True
                        logger.warning("ANEVOL 响应异常 -> {}".format(last_err))
                    elif body.get("code") == 1:
                        self._cb_report(True)
                        return str(body.get("answer", "")).strip()
                    else:
                        reason = self._reason_from_json(body) or "(无消息)"
                        last_err = "code={} | {}".format(body.get("code"), reason)
                        fatal = self._is_fatal_reason(reason)
                        service = False   # HTTP 200 的 code=0 一般是“题不在库里”，不算服务故障
                        stop = True       # 但明确的业务失败也没必要再试
                        if fatal:
                            logger.error("ANEVOL 拒绝请求(额度/付费问题) -> {}".format(last_err))
                        elif any(w in reason for w in _ANEVOL_NOTFOUND_WORDS):
                            # 纯粹的「题不在题库里」：这是正常业务结果，不是故障，别报 ERROR
                            # （必须放在 _ANEVOL_NO_RETRY_WORDS 之前判断，否则"答案不存在"
                            #   会被当成"密钥不存在"报成 ERROR）
                            logger.info("ANEVOL 未收录该题 -> {}".format(last_err))
                        elif any(w in reason for w in _ANEVOL_NO_RETRY_WORDS):
                            logger.error("ANEVOL 拒绝请求 -> {}（请检查套餐/密钥）".format(last_err))
                        else:
                            logger.warning("ANEVOL 返回失败 -> {}".format(last_err))
                else:
                    reason = self._reason_from_json(res)
                    last_err = "HTTP {}{}".format(
                        res.status_code, (" | " + reason) if reason else " " + res.text[:150]
                    )
                    fatal = self._is_fatal_reason(reason) or self._is_fatal_reason(res.text[:300])
                    service = True
                    # 能解析出原因 = ANEVOL 自己返回的业务错误（401/402/429/500），不是网络抖动
                    stop = bool(reason) or res.status_code in (401, 402, 403, 404, 429)
                    if fatal:
                        logger.error("ANEVOL 请求失败(额度/付费问题) -> {}".format(last_err))
                    else:
                        logger.warning("ANEVOL 请求失败 -> {}".format(last_err))
            except requests.exceptions.Timeout:
                # ANEVOL 正常要 20~25 秒，读超时说明它卡住了，再等一轮纯属浪费
                last_err = "ReadTimeout(>{:.0f}s)".format(_ANEVOL_READ_TIMEOUT)
                service = True
                stop = True
                logger.warning("ANEVOL 读超时 -> {}".format(last_err))
            except Exception as e:  # noqa: BLE001
                last_err = "{}: {}".format(type(e).__name__, e)
                service = True
                logger.warning("ANEVOL 请求异常 -> {}".format(last_err))

            fatal_any = fatal_any or fatal
            service_fail_any = service_fail_any or service
            if stop:
                break
            if attempt < max_attempts - 1:
                time.sleep(1.5)

        self._cb_report(False, fatal=fatal_any, count=service_fail_any)
        if service_fail_any or fatal_any:
            # 真正的服务故障 / 额度问题：值得记一条 ERROR
            if getattr(self, "_in_chain", False):
                logger.warning("ANEVOL 本次未查成，已交给下一个题库 -> {} | 题目: {}".format(
                    last_err, question[:30]))
            else:
                logger.error("ANEVOL 查询失败 -> {} | 题目: {}".format(last_err, question[:30]))
        else:
            # 只是题不在库里，具体原因上面已经按级别记过了，这里不重复刷屏
            logger.debug("ANEVOL 未命中（非服务故障）-> {} | 题目: {}".format(last_err, question[:30]))
        return None

    # ---------------- 题库入口 ----------------
    def _query(self, q_info: dict):
        qtype = _ANEVOL_TYPE_MAP.get(q_info.get("type", ""), "short_answer")
        options = [self._clean_option(o) for o in (q_info.get("options") or "").split("\n")]
        options = [o for o in options if o]

        ans = self._ask(q_info.get("title", ""), options, qtype)
        if not ans:
            return None

        if qtype in ("single", "multiple") and options:
            idx = self._parse_letters(ans, len(options))
            if idx is not None:
                picked = [options[i] for i in idx]
                if qtype == "single":
                    picked = picked[:1]
                    return self._safe_single(picked[0]) if picked else None
                return "\n".join(picked) if picked else None
            # 不是字母形式（例如直接返回了选项原文），原样返回
            return self._safe_single(ans) if qtype == "single" else ans

        if qtype == "judgement":
            return self._normalize_judgement(ans, options)

        return ans


# ---------------------------------------------------------------------------
# 网课小工具题库（GO题，https://cx.icodef.com/）
# ---------------------------------------------------------------------------
_ICODEF_DEFAULT_API = "https://cx.icodef.com/wyn-nb?v=4"
_ICODEF_MIN_INTERVAL_DEFAULT = 0.3      # 串行前提下的最小请求间隔(秒)
_ICODEF_READ_TIMEOUT_DEFAULT = 15

# code=1 但内容其实是「没搜到」的兜底判定
_ICODEF_EMPTY_WORDS = ("未搜索到答案", "未找到", "没有找到")

# 服务级故障连续多少次后熔断（题不在库里是 code=-1，不计入）
_ICODEF_CB_THRESHOLD = 5
_ICODEF_CB_PAUSE_BASE = 60
_ICODEF_CB_PAUSE_MAX = 300


class TikuIcodef(_AnswerNormalizeMixin, Tiku):
    """
    网课小工具题库（GO题，https://cx.icodef.com/）内置实现，无需任何本地桥接。

    配置（config.ini 的 [tiku] 段）：
        icodef_url           = https://cx.icodef.com/wyn-nb?v=4  ; 可留空走默认值
        icodef_authorization = 可选，填了会带上 Authorization 头
        icodef_min_interval  = 0.3    ; 请求最小间隔(秒)
        icodef_timeout       = 15     ; 读超时(秒)

    实测结论（三条都已经在代码里处理）：
    1. 官方限制「1 并发」，并发请求会被限流：
       {"code":-2,"data":"...触发流控限制: 1并发限制"}。
       而 chaoxing 默认 jobs=8，所以这里用类级锁把所有请求整体串行化，
       被限流时再退避重试；锁要一直握到拿到响应为止，否则等于没限并发。
    2. 返回体：code=1 才有答案，data 是明文答案（多选题用 '#' 连接多个）；
       code=-1 是「题不在库里」，属于正常业务结果，不计入熔断；code=-2 才是服务级故障。
    3. data 给的是选项原文而不是字母，所以要走 _normalize_text_answer 归一化。
    """

    # 类级，跨实例生效：整个进程同时只允许一个 icodef 请求在飞
    _serial_lock = threading.Lock()
    _serial_last_call = 0.0

    def __init__(self) -> None:
        super().__init__()
        self.name = "网课小工具题库"
        self.api = _ICODEF_DEFAULT_API
        self.authorization = ""
        self.min_interval = _ICODEF_MIN_INTERVAL_DEFAULT
        self.read_timeout = _ICODEF_READ_TIMEOUT_DEFAULT
        # 熔断器，和 ANEVOL 的一样：服务挂了就先停一会儿，别把整章拖死
        self._consec_fail = 0
        self._pause_until = 0.0
        self._pause_round = 0
        self._cb_lock = threading.Lock()

    # ---------------- 配置 ----------------
    def _init_tiku(self):
        api = (self._conf.get("icodef_url") or "").strip()
        if not api:
            # 兼容误把地址写进共用 url 字段的情况（但不能是本地桥接地址）
            cfg_url = (self._conf.get("url") or "").strip()
            if cfg_url and "127.0.0.1" not in cfg_url and "localhost" not in cfg_url:
                api = cfg_url
        api = api or _ICODEF_DEFAULT_API
        if "?" not in api and "wyn-nb" in api:
            api = api.rstrip("/") + "?v=4"
        self.api = api

        self.authorization = (self._conf.get("icodef_authorization") or "").strip()

        try:
            self.min_interval = float(self._conf.get("icodef_min_interval") or _ICODEF_MIN_INTERVAL_DEFAULT)
        except (TypeError, ValueError):
            self.min_interval = _ICODEF_MIN_INTERVAL_DEFAULT
        self.min_interval = max(0.0, self.min_interval)

        try:
            self.read_timeout = float(self._conf.get("icodef_timeout") or _ICODEF_READ_TIMEOUT_DEFAULT)
        except (TypeError, ValueError):
            self.read_timeout = _ICODEF_READ_TIMEOUT_DEFAULT

        logger.info(f"网课小工具题库已加载 -> 接口: {self.api} (串行 1 并发, 最小间隔 {self.min_interval}s)")

    # ---------------- 熔断 ----------------
    def _pause_remaining(self) -> float:
        with self._cb_lock:
            return max(0.0, self._pause_until - time.time())

    def _cb_report(self, ok: bool, count: bool = True):
        """
        ok    : 本次服务是否正常（题搜不到也算正常）
        count : 是否计入熔断。只有服务级故障才计，题搜不到不算。
        """
        with self._cb_lock:
            if ok:
                self._consec_fail = 0
                self._pause_round = 0
                return
            if not count:
                return
            self._consec_fail += 1
            if self._consec_fail >= _ICODEF_CB_THRESHOLD and self._pause_until < time.time():
                self._pause_round = min(self._pause_round + 1, 4)
                pause = min(_ICODEF_CB_PAUSE_BASE * (2 ** (self._pause_round - 1)), _ICODEF_CB_PAUSE_MAX)
                self._pause_until = time.time() + pause
                self._consec_fail = 0
                logger.warning(
                    "网课小工具题库连续 {} 次不可用，暂停 {} 秒不再请求"
                    "（这几道题会走随机作答，覆盖率不达标不会提交；服务恢复后会自动继续）".format(
                        _ICODEF_CB_THRESHOLD, pause)
                )

    # ---------------- 请求 ----------------
    def _ask(self, question):
        """调用网课小工具题库，成功返回答案字符串，失败返回 None。"""
        if not question:
            return None

        remaining = self._pause_remaining()
        if remaining > 0:
            logger.debug("网课小工具题库熔断中，本次跳过查询（还剩 {:.0f} 秒）".format(remaining))
            return None

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.authorization:
            headers["Authorization"] = self.authorization

        max_attempts = 3
        last_err = None
        service_fail = False

        for attempt in range(max_attempts):
            stop = False          # 本次是否立刻终止（不再重试）
            service = False       # 是否属于服务级故障
            retry_wait = 1.0
            try:
                # 官方限「1 并发」：锁必须一直握到拿到响应，否则等于没串行化
                with TikuIcodef._serial_lock:
                    wait = (TikuIcodef._serial_last_call + self.min_interval) - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    try:
                        res = requests.post(
                            self.api,
                            data={"question": question},
                            headers=headers,
                            timeout=(5, self.read_timeout),
                            verify=False,
                        )
                    finally:
                        TikuIcodef._serial_last_call = time.time()

                if res.status_code == 200:
                    body = None
                    try:
                        body = res.json()
                    except Exception:
                        pass
                    if not isinstance(body, dict):
                        last_err = "响应不是合法JSON: " + res.text[:120]
                        service = True
                        stop = True
                        logger.warning("网课小工具题库响应异常 -> {}".format(last_err))
                    else:
                        code = body.get("code")
                        msg = str(body.get("msg") or body.get("message") or "").strip()
                        if str(code) == "1":
                            data = body.get("data")
                            if isinstance(data, (list, tuple)):
                                data = "#".join(str(x) for x in data)
                            data = str(data or "").strip()
                            if data and not any(w in data for w in _ICODEF_EMPTY_WORDS):
                                self._cb_report(True)
                                return data
                            # code=1 但内容是「没搜到」：正常业务结果，不计熔断
                            self._cb_report(True)
                            logger.info("网课小工具题库未收录该题 -> {}".format(question[:30]))
                            return None
                        elif str(code) == "-1":
                            # 题不在库里：正常业务结果，服务本身是健康的
                            self._cb_report(True)
                            logger.info("网课小工具题库未收录该题 -> {}".format(question[:30]))
                            return None
                        elif str(code) == "-2" or "流控" in msg:
                            # 触发流控：退避重试，计入熔断
                            service = True
                            retry_wait = 2.0
                            last_err = "code={} 触发流控 | {}".format(code, msg)
                            logger.warning("网课小工具题库触发流控 -> {}".format(last_err))
                        else:
                            service = True
                            stop = True
                            last_err = "code={} | {}".format(code, msg or "(无消息)")
                            logger.warning("网课小工具题库返回失败 -> {}".format(last_err))
                else:
                    service = True
                    # 5xx 属基础设施抖动，值得重试（响应很快，代价只有 2 秒退避）；
                    # 401/402/403/404 这类重试无意义，直接放弃。
                    stop = res.status_code in (401, 402, 403, 404)
                    last_err = "HTTP {} {}".format(res.status_code, res.text[:120])
                    logger.warning("网课小工具题库请求失败 -> {}".format(last_err))
            except requests.exceptions.Timeout:
                service = True
                stop = True
                last_err = "ReadTimeout(>{}s)".format(self.read_timeout)
                logger.warning("网课小工具题库读超时 -> {}".format(last_err))
            except Exception as e:  # noqa: BLE001
                service = True
                last_err = "{}: {}".format(type(e).__name__, e)
                logger.warning("网课小工具题库请求异常 -> {}".format(last_err))

            service_fail = service_fail or service
            if stop or not service:
                break
            if attempt < max_attempts - 1:
                time.sleep(retry_wait)

        self._cb_report(False, count=service_fail)
        # 走到这里一定是服务级故障（"题不在库里"的 code=-1 前面就 return 了）。
        # 在题库链里它只是「这一环没查成」，链条还会问下一个，所以降为 WARNING；
        # 单独使用时才是 ERROR。
        if getattr(self, "_in_chain", False):
            logger.warning("网课小工具题库本次未查成，已交给下一个题库 -> {} | 题目: {}".format(
                last_err, question[:30]))
        else:
            logger.error("网课小工具题库查询失败 -> {} | 题目: {}".format(last_err, question[:30]))
        return None

    # ---------------- 题库入口 ----------------
    def _query(self, q_info: dict):
        qtype = _ANEVOL_TYPE_MAP.get(q_info.get("type", ""), "short_answer")
        options = self._split_options(q_info)

        ans = self._ask(q_info.get("title", ""))
        if not ans:
            return None

        return self._normalize_text_answer(ans, qtype, options)


class TikuChain(Tiku):
    """
    多题库顺序回退（config.ini 里 provider 写多个类名即可启用）：
        provider = TikuIcodef,TikuAnevol
    按顺序查询，第一个给出「类型校验通过」的答案的题库胜出，后面的不再请求。

    好处：先问又快又免费的，只有它搜不到时才去问慢的/花钱的。
    每个子题库各自保留自己的缓存、限流和熔断，互不影响。
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = "题库链"
        self.providers = []
        self.provider_names = []
        self._is_chain = True

    def _init_tiku(self):
        raw = self._conf.get("provider") or ""
        names = [n.strip() for n in re.split(r"[,;，、\s]+", raw) if n.strip()]

        for cls_name in names:
            cls = globals().get(cls_name)
            if not isinstance(cls, type) or not issubclass(cls, Tiku) or cls is TikuChain:
                logger.error("题库链里的 '{}' 不是有效的题库类名，已跳过".format(cls_name))
                continue
            try:
                inst = cls()
                inst.config_set(self._conf)
                inst.init_tiku()
            except Exception as e:  # noqa: BLE001
                logger.error("题库 {} 初始化失败，已跳过 -> {}: {}".format(cls_name, type(e).__name__, e))
                continue
            if getattr(inst, "DISABLE", False):
                logger.warning("题库 {} 初始化后处于停用状态，已跳过".format(cls_name))
                continue
            # 标记为「链上的子题库」：它没查到答案时只记 debug，
            # 免得日志里一堆 ERROR 让人以为整条链挂了（其实后面还有兜底）
            inst._in_chain = True
            self.providers.append(inst)
            self.provider_names.append(cls_name)

        if not self.providers:
            self.DISABLE = True
            logger.error("题库链里没有任何可用题库，题库功能已停用（将只做视频/文档任务点）")
        else:
            logger.info("题库链已加载，查询顺序: {} （前一个搜不到才会问下一个）".format(
                " -> ".join(self.provider_names)))

    def _query(self, q_info: dict):
        for inst in self.providers:
            try:
                ans = inst.query(q_info)
            except Exception as e:  # noqa: BLE001
                # 单个题库炸了不能拖垮整条链，继续问下一个
                logger.error("题库 {} 查询异常，继续尝试下一个 -> {}: {}".format(
                    inst.name, type(e).__name__, e))
                continue
            if ans:
                # 明确写出是哪个题库/AI 答出来的，出问题时好定位
                logger.info("题库链命中：{}".format(inst.name))
                return ans
        return None

