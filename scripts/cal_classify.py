#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 大事日历分类与口径规范化
====================================

给 us-cal-data 的每条事件补四个分类指标，并把同一类条目的写法收敛到统一口径。

三个指标（详细定义见 docs/日历分类指标.md）：

  cat     一级范畴，决定格子色条与图例，4 值：econ / pol / dip / mkt
  topic   议题域，用于检索与聚合，11 值：price / labor / growth / trade / money /
          fiscal / tech / energy / security / election / calendar
  actor   主导主体，8 值：exec / agency / congress / fed / court / stats / intl / none

为什么要有这套东西：原方案只有 econ/pol/dip/hol 四类，结果「美国对外投资安全计划
拟议规则意见期截止」这类行政规则制定被塞进 econ，「政治」只剩 12 条选举与国会程序；
同一类条目（如联邦假日）标题后缀、备注句式、来源写法各写各的，无法横向比较。

用法：
    python3 scripts/cal_classify.py check             # 体检：覆盖、取值合法性、跨类冲突
    python3 scripts/cal_classify.py report            # 打印分类结果（按范畴分组）
    python3 scripts/cal_classify.py report --csv out.csv
    python3 scripts/cal_classify.py apply             # 写入 index.html（幂等）
    python3 scripts/cal_classify.py apply --fix-holiday   # 同时统一假日标题与备注口径

退出码：check 0 合规 / 1 有问题；其余 0 完成
"""

import argparse
import collections
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PORTAL = os.path.join(ROOT, 'index.html')
BLOCK_RE = re.compile(
    r'(<script type="application/json" id="us-cal-data"[^>]*>)(.*?)(</script>)', re.S)

# ---------------------------------------------------------------- 指标定义

CAT_DEF = collections.OrderedDict([
    ('econ', '经济'),
    ('pol', '政治与治理'),
    ('dip', '外交与安全'),
    ('mkt', '交易日历'),
])

TOPIC_DEF = collections.OrderedDict([
    ('price', '物价'),
    ('labor', '就业与薪酬'),
    ('growth', '增长与消费'),
    ('trade', '贸易与外部平衡'),
    ('money', '货币与金融监管'),
    ('fiscal', '财政与住房'),
    ('tech', '科技与投资安全'),
    ('energy', '能源与环境'),
    ('security', '安全与地缘'),
    ('election', '选举与政治程序'),
    ('calendar', '假期与时制'),
])

ACTOR_DEF = collections.OrderedDict([
    ('exec', '行政部门'),
    ('agency', '独立监管机构'),
    ('congress', '国会'),
    ('fed', '美联储'),
    ('court', '司法'),
    ('stats', '统计机构'),
    ('intl', '外国政府或多边组织'),
    ('none', '无主导主体'),
])

# ---------------------------------------------------------------- 分类规则

# 统计发布：按发布机构与指标族识别，顺序即优先级
STAT_RULES = [
    (r'非农就业报告', 'labor'),
    (r'CPI', 'price'),
    (r'PPI', 'price'),
    (r'PCE', 'price'),
    (r'GDP', 'growth'),
    (r'零售电商', 'growth'),
    (r'零售销售', 'growth'),
    (r'国际交易与对外投资头寸', 'growth'),
    (r'就业成本指数', 'labor'),
    (r'薪酬成本调查', 'labor'),
    (r'贸易差额', 'trade'),
    (r'进出口价格', 'trade'),
]

# 外交与安全：多边机制与双边磋商节点。议题按机制的**主要议程**定，不一律归到「安全」
DIP_RULES = [
    (r'Quad|四方安全对话', 'security', 'intl'),
    (r'慕尼黑安全会议', 'security', 'intl'),
    (r'东盟', 'security', 'intl'),
    (r'G20.*外长', 'security', 'intl'),
    (r'G20', 'money', 'intl'),                 # 领导人峰会、财长会与 Sherpa 会以宏观经济议程为主
    (r'IMF|世界银行', 'money', 'intl'),
    (r'APEC', 'trade', 'intl'),
    (r'达沃斯', 'money', 'intl'),              # 世界经济论坛，议题以宏观与商业为主
    (r'COP\d', 'energy', 'intl'),
    (r'关税休战', 'trade', 'exec'),
    (r'以色列', 'election', 'intl'),
]

# 政治与治理：选举与国会程序、行政规则制定、贸易救济措施
POL_RULES = [
    (r'中期选举日|大选日', 'election', 'exec'),
    (r'就职典礼', 'election', 'exec'),
    (r'国会开幕|国会联席会议|计点选举人票', 'election', 'congress'),
    (r'选举人', 'election', 'exec'),
    (r'拨款截止', 'fiscal', 'congress'),
    (r'USMCA', 'trade', 'exec'),
    (r'301 条款|关税暂停', 'trade', 'exec'),
    (r'对外投资安全计划', 'tech', 'exec'),
    (r'压力测试|资本计划规则|资本缓冲', 'money', 'fed'),
    (r'稳定币|加密资产', 'money', 'agency'),
    (r'EPA|可再生燃料|三氯乙烯', 'energy', 'agency'),
    (r'住房法|机构购房', 'fiscal', 'exec'),
    (r'意见期|规则生效|新规', 'money', 'agency'),
]

# 个别条目的手工归类：规则判错或需要专门口径时在此覆盖，键为 (日期, 标题)
OVERRIDES = {
    ('2026-11-02', '美联储压力测试透明度新规生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'fed'},
    ('2026-12-01', '美联储资本计划规则与压力资本缓冲修订生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'fed'},
    ('2027-01-18', 'OCC 稳定币发行实施规则生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency'},
    ('2027-01-09', '301 条款造船等行业关税暂停拟延至 1 月 9 日'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},
    ('2027-07-01', 'USMCA 2027 年度联合审查'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},
    ('2026-12-11', '联邦政府拨款截止日'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'congress'},
    ('2026-11-03', '2026 年中期选举日'):
        {'cat': 'pol', 'topic': 'election', 'actor': 'exec'},
    ('2028-11-07', '2028 年美国总统与国会大选日'):
        {'cat': 'pol', 'topic': 'election', 'actor': 'exec'},
    ('2029-01-20', '美国总统就职典礼'):
        {'cat': 'pol', 'topic': 'election', 'actor': 'exec'},
    ('2027-01-10', '中美关税休战延期期满'):
        {'cat': 'dip', 'topic': 'trade', 'actor': 'exec'},
    # —— 2026-11-01 日更新增：规则兜底会把「意见期/规则生效」一律判成 money，
    #    但下列条目的主议题并非金融监管，按议题实际归属手工归类 ——       # 频谱与卫星通信属科技议题     # 联邦支付政策归财政      # 进口申报规则归贸易      # 免税包裹申报规则归贸易     # LNG 授权归能源
    ('2027-01-01', 'CMS 2027 日历年度 Medicare 终末期肾病等支付规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},     # 医保报销规则归财政
    ('2027-02-01', '美国总统向国会提交 2028 财年预算案（法定最迟日）'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'exec'},       # 预算案由行政部门提交
    # —— 2026-11-02 日更新增：本批为联邦公报 09—11 月刊发的监管节点，
    #    兜底规则会把「意见期/生效」一律判成 money/agency，逐条按所监管对象领域覆盖 ——
    ('2026-11-09', 'IRS 车贷利息扣除最终规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'exec'},
    ('2026-11-30', 'CMS GLOBE 药品定价模型生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},
    ('2026-11-30', 'NHTSA CAFE 规则 III 生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},
    ('2026-12-14', 'EPA TSCA 重要新用途规则 26-1 生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},
    ('2026-12-14', 'EPA 清洁水法设施应急计划合规日延期生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},
    ('2027-01-04', '劳工部 PBM 费用披露规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},
    # —— 2026-11-03 日更新增：6 条监管规则 + 1 条 232 条款总统公告 + 1 条外交节点。
    #    兜底规则把含「意见期/规则生效」的条目一律判成 money/agency，本批按所监管
    #    对象领域逐条覆盖；俄乌会谈、G7 接任、232 报告期限三类标题不匹配任何规则，
    #    必须显式覆盖，否则 classify() 返回空值、check 判为不一致 ——      # 非移民雇员身份与居留衔接，属用工议题      # 联邦采购与扶持项目门槛，归财政      # 银行准入与展业规则
    ('2026-11-30', 'USCIS EB-5 移民投资费用规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 政府收费调整      # 消费品规格标准，归消费
    ('2027-04-26', '232 条款机器人与工业机械进口谈判 180 日报告期限'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 美方单边贸易措施程序节点
    ('2026-11-10', '美方斡旋的俄乌工作层级会谈（阿布扎比）'):
        {'cat': 'dip', 'topic': 'security', 'actor': 'exec'},      # 美方斡旋的俄乌谈判
    ('2027-01-01', '美国接任 G7 主席国'):
        {'cat': 'dip', 'topic': 'money', 'actor': 'exec'},         # G7 议程以宏观经济为主，同 G20 口径
    # —— 2026-11-04 日更新增：5 条第 232 条总统公告 + 18 条监管规则。
    #    232 公告标题不含「意见期/规则生效」，不匹配任何规则，必须显式覆盖；
    #    监管规则一律被兜底判成 money/agency，按所监管对象领域逐条改写。
    #    医保交易所资格（2026-11-27）、EPA 温室气体报告（2026-11-30）、
    #    医保价格透明度（2026-12-07）三条与规则判定一致，不设覆盖 ——
    ('2026-12-04', '232 条款多晶硅及衍生品最低进口价与 15% 从价税生效'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 232 关税措施，同 2027-04-26 机器人条口径
    ('2027-01-01', '232 条款木材制品关税税率上调生效'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 232 提税时点
    ('2027-02-09', '232 条款无人机关键零部件 25% 关税生效'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 232 零部件关税时点
    ('2027-01-05', '232 条款商用飞机与航发进口谈判 180 日报告期限'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 232 谈判程序节点
    ('2027-04-20', '232 条款风电设备进口谈判 180 日报告期限'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 232 谈判程序节点      # 跨境资金税务处理，归税收       # 联邦雇员休假资格，属用工议题       # 进口准入与检疫条件       # 商船建造与航运扶持       # 食品供应链记录与进口合规        # 自动驾驶车辆认证标准
    ('2026-11-25', 'FCC 视频节目闭路字幕规则生效'):
        {'cat': 'pol', 'topic': 'tech', 'actor': 'agency'},        # 电信与媒体监管
    ('2026-11-25', 'NRC 采认能源部与战争部既有授权规则生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 核反应堆许可      # 教育捐赠税收抵免
    ('2026-12-01', 'IRS 联邦奖学金税收抵免临时规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 同上，临时规则配套
    ('2026-12-03', 'NRC 许可争议裁决程序简化规则生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 核设施许可程序       # 航运救济与进口限制       # 跨境运输申报       # 肉品进口条件       # 跨境危险品运输规则      # LNG 设施安全
    ('2027-01-04', 'CMS 器官获取组织覆盖条件修订生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency'},       # 医保支付与供给准入
    ('2027-01-04', 'HHS 残障歧视规则修订生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 受资助项目用工与准入义务
    # —— 2026-11-05 日更新增：24 条新条目（20 条联邦监管规则生效节点 + 3 条授权与
    #    许可到期 + 1 条合规日延期）。兜底规则把含「规则生效」的条目一律判成
    #    money/agency，标题不含「规则生效」的条目则落空、check 会判为无法归类，
    #    故逐条按所监管对象领域或事项性质显式覆盖 ——
    ('2026-11-12', 'EPA 科罗拉多 Class VI 注入井首要权限生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 碳封存与地下注入控制
    ('2026-11-16', '劳工部失业补偿数据披露规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 失业保险用工与稽核事项
    ('2026-11-18', 'OPM 行政休假与人员重组规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 联邦雇员休假与用工事项
    ('2026-11-18', 'OPM 高级专业人员绩效管理规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 联邦雇员考评与任用事项
    ('2026-11-25', 'FCC 机器人电话同意撤销规则生效'):
        {'cat': 'pol', 'topic': 'tech', 'actor': 'agency'},        # 电信与外呼管理
    ('2026-11-27', 'EPA 甲醛复合木制品标准更新生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 室内空气与建材环保标准
    ('2026-11-27', 'DOJ 枪支经销商雇员背景核查规则生效'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'agency'},    # 枪支管理与背景核查
    ('2026-11-30', 'OPM 战略人力资源管理规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 联邦机构人力资本管理
    ('2026-11-30', 'HHS 子女抚养执行减负规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 社会福利项目执行与分担
    ('2026-12-04', 'NCUA 法定留置权移除规则生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency'},       # 信用合作社担保权益
    ('2026-12-07', 'DOE 商用暖风炉能效测试程序撤回生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 商用暖通设备能效
    ('2026-12-14', 'OPM 家庭与医疗休假法修订生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 联邦雇员休假资格
    ('2026-12-14', 'FDA 食盐替代品标准生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 食品与药品监管，循库内健康类归 fiscal 口径
    ('2026-12-21', 'NRC 核材料许可豁免规则生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 核材料许可与豁免
    ('2026-12-28', 'NRC AP1000 设计认证续期生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 核电机组设计认证
    ('2026-12-28', 'HHS 健康信息技术去监管规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 医疗信息化与支付衔接
    ('2026-12-28', 'USDA 农宅担保贷款 ADU 条款生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 住房与农村抵押融资
    ('2026-12-29', 'CPSC 车库门安全标准生效'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'agency'},    # 消费产品安全标准
    ('2027-01-19', 'OPM 取消职级晋升限制规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 联邦职位晋升与用工事项
    ('2027-07-01', '教育部认证改革规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 学历认证与教育劳动力事项
    ('2026-12-31', 'EXIM 出口信贷授权与 CTEP 计划届满'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'congress'},    # 须国会重新立法授权
    ('2026-12-31', '柴油燃油紧急税收减免到期'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'exec'},        # 行政部门税收减免期届满
    ('2027-04-07', 'OFAC 第 135 号通用许可（俄产柴油）到期'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 美方单边制裁许可期限
    ('2027-03-01', 'DOE 新建筑化石燃料限制废止合规日延期生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 联邦建筑清洁能源合规
    # 2026-11-06 批次：总统公告的行政行为 + 联邦监管规则
    ('2026-11-05', '美国延续四项国家紧急状态'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'exec'},      # 美方单边制裁与对外投资限制的年度延续
    ('2026-11-20', '美国消音器出口管制改由 EAR 统一实施'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'agency'},    # 军品与两用物项出口管制轨道转换
    ('2026-12-07', 'FCC 卫星宽带频谱共享现代化规则生效'):
        {'cat': 'pol', 'topic': 'tech', 'actor': 'agency'},        # 频谱与卫星宽带
    ('2026-12-28', 'DOE 联邦资助项目反歧视规定废止生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 联邦资助项目用工与合规义务
    ('2027-01-04', 'EPA 重要新用途规则 25-3.5e 生效'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # TSCA 化学品监管
    ('2027-01-15', 'FDA 撤销 FD&C 红 3 号色素使用规定生效'):
        {'cat': 'pol', 'topic': 'growth', 'actor': 'agency'},      # 食品与药品配料规格
    ('2027-02-01', '交通部航空延误与取消原因分类规则生效'):
        {'cat': 'pol', 'topic': 'growth', 'actor': 'agency'},      # 航空运输的报送口径
    ('2027-02-04', 'FDA 非临床试验术语规则生效'):
        {'cat': 'pol', 'topic': 'growth', 'actor': 'agency'},      # 药品非临床研究提交规范
    ('2027-07-01', '教育部学生贷款与收入问责规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 联邦学生贷款与院校资格
    ('2027-12-31', '232 条款铝钢铜关税过渡税率届满'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 金属进口关税的阶段性安排
    ('2028-01-01', '232 条款铝钢铜关税切回基准税率'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 同上，税率切换节点
    ('2028-01-01', 'FinCEN 投资顾问反洗钱规则生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency'},       # 金融监管（反洗钱）
    ('2033-03-07', 'FDA 药品国家药品码格式统一规则生效'):
        {'cat': 'pol', 'topic': 'growth', 'actor': 'agency'},      # 药品标识与追溯编码
    # 2026-11-07 批次：补入联邦公报 2026-11-06 新刊期与前几轮遗漏的已公布节点。
    # 兜底规则把含「规则生效 / 新规」的条目一律判 money/agency，且不读条目自带指标，
    # 故本批 14 条全部手工归类。
    ('2026-10-19', '美国延续三项国家紧急状态'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'exec'},      # 制裁类国家紧急状态的年度延续
    ('2026-11-10', 'BIS 实体清单关联方规则恢复实施'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'agency'},    # 两用物项出口管制的关联方穿透规则
    ('2026-12-07', 'USDA WIC 线上订购与食品配送规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 联邦营养援助福利计划
    ('2026-12-31', '后量子密码迁移试点完成期限'):
        {'cat': 'pol', 'topic': 'tech', 'actor': 'exec'},          # 联邦信息系统密码技术迁移
    ('2027-01-01', '国防供应链豁免终止'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'exec'},      # 国防生产法下的采购豁免
    ('2027-01-01', 'CMS 医院门诊与手术中心支付规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 医保支付规则（与库内 CMS 口径一致）
    ('2027-01-05', 'CBP 外国进口记录人强化要求生效'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'agency'},       # 进口申报与记录人责任
    ('2027-01-05', '劳工部注册学徒计划反歧视规则生效'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'agency'},       # 学徒用工与反歧视义务
    ('2027-01-20', 'CBP 邮政小额豁免正式报关合规期延至 1 月 20 日'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'agency'},       # 直邮进口申报合规期限
    ('2027-04-02', '仿制药关税复审报告期限'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 232 条款适用范围的复审报告节点
    ('2027-09-14', '对古巴敌国贸易法授权延续届满'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'exec'},      # 敌国贸易法授权的年度延续
    ('2027-09-21', 'H-1B 非移民工人入境限制公告延长届满'):
        {'cat': 'pol', 'topic': 'labor', 'actor': 'exec'},         # 外籍技术劳工准入限制
    ('2029-01-20', '232 条款药品关税零税率适用截止'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 药品关税的阶段性优惠截止
    ('2030-04-02', '232 条款药品关税税率上调至 100%'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec'},         # 药品关税税率上调节点
    # 2026-11-08 批次：14 条新节点（联邦公报已公布的规则生效/失效/合规期限 + 一条中美双边通话）。
    # 兜底规则把含「规则生效 / 新规」的条目一律判 money/agency，且不读条目自带指标，
    # 故本批 14 条全部手工归类。
    ('2026-11-06', '中美经贸牵头人视频通话'):
        {'cat': 'dip', 'topic': 'trade', 'actor': 'exec'},          # 中美经贸磋商机制的双边节点
    ('2026-12-03', 'BIS 多晶硅囤积限制规则届满'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'agency'},       # 多晶硅进口调整前的囤积监控规则
    ('2026-12-31', 'BIS 集成电路设计者资格申请截止'):
        {'cat': 'pol', 'topic': 'tech', 'actor': 'agency'},        # 半导体设计者的出口许可资格
    ('2027-02-01', 'CMS 保险代理人与经纪商注册临时禁令届满'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 联邦医疗保险市场准入（与库内 CMS 口径一致）
    ('2027-02-04', 'SEC 投资公司小企业与小组织定义修订生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency'},       # 基金与投资顾问的小实体认定
    ('2027-03-10', 'EPA 温室气体报告报送期限延至 3 月 10 日'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 温室气体排放数据报送
    ('2027-06-16', 'CMS 医保认证机构监管与利益冲突规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 医保资质认定与支付链条
    ('2027-06-21', 'EPA PCE 与 CTC 初始监测合规期限'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # TSCA 化学品工作场所保护计划
    ('2027-07-04', 'FCC 上层 C 波段竞争性拍卖法定截止日'):
        {'cat': 'pol', 'topic': 'tech', 'actor': 'agency'},        # 频谱拍卖与 5G 频谱供给
    ('2027-08-27', 'BIS 黑粉与钨废料销售分配令届满'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'agency'},    # 国防生产法下的关键矿产分配与出口限制
    ('2027-09-20', 'EPA PCE 与 CTC 暴露限值与呼吸防护合规期限'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # TSCA 化学品职业暴露合规
    ('2027-12-20', 'EPA PCE 暴露控制计划合规期限'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # TSCA 化学品暴露控制计划
    ('2028-01-01', 'CFPB 小企业贷款数据报送合规期限'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency'},       # ECOA 小企业信贷数据报送（金融监管）
    ('2030-12-31', 'EPA 南加州 PM2.5 达标期限延至'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency'},      # 空气质量达标期限（EPA 规则）
    # 2026-11-10 批次：4 条。G7 无对应 DIP_RULES 规则（会被判空），CMS 与 BIS 条目会被兜底判
    # money/agency 或判空，故逐条手工归类。
    ('2026-11-09', 'G7 外长会议（巴黎）'):
        {'cat': 'dip', 'topic': 'security', 'actor': 'intl'},      # 七国集团外长会，美国为成员方
    ('2026-12-05', 'CMS 联邦医疗保险药品价格谈判规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 医保药价谈判（与库内 CMS 口径一致）
    ('2027-01-01', 'CMS 2027 年医师费用表支付规则生效'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'agency'},      # 医师费用表与 B 部分支付规则
    ('2027-01-11', 'BIS 实体清单关联方规则恢复实施'):
        {'cat': 'pol', 'topic': 'security', 'actor': 'agency'},    # 出口管制关联方许可要求
}

# 统计发布的市场传导强度：同一指标族内部按「初值 > 修正 > 终值」「总量 > 分项」区分
IMPACT_BY_FAMILY = [
    (r'非农就业报告|CPI|PCE 物价指数|FOMC 议息会议', 'high'),
    (r'GDP 初值', 'high'),
    (r'GDP', 'mid'),
    (r'PPI|零售销售|贸易差额|就业成本指数|薪酬成本调查|进出口价格|零售电商|国际交易', 'mid'),
]


def classify(ev):
    """按规则给出三项指标；返回 dict（含 _from 说明依据，便于复核）。"""
    title = ev.get('title') or ''
    date = ev.get('d') or ''
    key = (date, title)
    if key in OVERRIDES:
        out = dict(OVERRIDES[key])
        out['_from'] = 'override'
        return out

    # ① 交易日历：联邦假日、休市安排与时制切换
    if ev.get('cat') in ('hol', 'mkt') or '联邦假日' in title:
        return {'cat': 'mkt', 'topic': 'calendar', 'actor': 'none', '_from': 'holiday'}
    if '冬令时' in title or '夏令时' in title:
        return {'cat': 'mkt', 'topic': 'calendar', 'actor': 'none', '_from': 'dst'}

    # ② 货币政策
    if 'FOMC' in title:
        return {'cat': 'econ', 'topic': 'money', 'actor': 'fed', '_from': 'fomc'}

    # ③ 统计发布
    for pat, topic in STAT_RULES:
        if re.search(pat, title):
            # 普查局与 BEA 联合发布，统一记统计机构
            return {'cat': 'econ', 'topic': topic, 'actor': 'stats',
                    '_from': 'stat:' + pat}

    # ④ 外交与安全
    for pat, topic, actor in DIP_RULES:
        if re.search(pat, title):
            return {'cat': 'dip', 'topic': topic, 'actor': actor, '_from': 'dip:' + pat}

    # ⑤ 政治与治理
    for pat, topic, actor in POL_RULES:
        if re.search(pat, title):
            return {'cat': 'pol', 'topic': topic, 'actor': actor, '_from': 'pol:' + pat}

    return {'cat': '', 'topic': '', 'actor': '', '_from': 'none'}


# ---------------------------------------------------------------- 假日口径

# 美股全天休市的假日族（NYSE/Nasdaq）。哥伦布日、退伍军人节照常交易，不在表内。
MARKET_CLOSED = ('元旦', '马丁·路德·金', '总统日', '华盛顿诞辰', '耶稣受难',
                 '阵亡将士', '六月节', '独立日', '劳工节', '感恩节', '圣诞')

# 归并标题时会丢掉的法定名称，补进备注（标题保持统一格式，事实不丢）
NAME_ALIAS = {'总统日': "法定名称为华盛顿诞辰日（Washington's Birthday）"}


def holiday_is_closed(ev):
    """按 note 的事实陈述判定该年该日的休市状态，判断不了才回落到假日族表。

    个别年份有例外：2027-12-31 是 2028 年元旦的联邦观察日，纽交所当日照常交易。
    """
    note = ev.get('note') or ''
    pos_closed = note.find('美股休市')
    pos_open = note.find('美股正常交易')
    if pos_closed >= 0 and (pos_open < 0 or pos_closed < pos_open):
        return True
    if pos_open >= 0:
        return False
    name = re.sub(r'（[^）]*）', '', ev.get('title') or '')
    return any(k in name for k in MARKET_CLOSED)


def fix_holiday(ev):
    """统一假日条目的 title 与 note 口径，返回改动 dict（可能为空）。

    标题统一为「节日名（联邦假日）」/「节日名（联邦假日，美股休市）」；
    备注去掉与标题重复的「联邦假日」段、统一「周一」写法与来源全称。
    """
    if ev.get('cat') not in ('hol', 'mkt') and '联邦假日' not in (ev.get('title') or ''):
        return {}
    closed = holiday_is_closed(ev)
    raw_title = ev.get('title') or ''
    name = re.sub(r'（[^）]*）', '', raw_title).strip()
    name = name.replace('华盛顿诞辰日', '总统日').replace('美国独立日', '独立日')
    new_title = '%s（联邦假日%s）' % (name, '，美股休市' if closed else '')

    segs = [x.strip() for x in re.split(r'\s*·\s*', ev.get('note') or '') if x.strip()]
    body, src = [], ''
    for s in segs:
        m = re.match(r'^来源\s*[:：]\s*(.+)$', s)
        if m:
            src = m.group(1).strip()
            continue
        if re.fullmatch(r'联邦假日(（[^）]*）)?', s):
            continue
        s = re.sub(r'^联邦假日[，,]\s*', '', s)
        s = re.sub(r'星期([一二三四五六日天])', r'周\1', s)
        if s:
            body.append(s)
    if not body:
        body = ['美股休市' if closed else '美股正常交易']
    alias = NAME_ALIAS.get(name)
    if alias and not any('法定名称' in x for x in body):
        body.append(alias)
    src = re.sub(r'^美国人事管理局联邦假日安排', '美国人事管理局（OPM）联邦假日安排', src)
    new_note = ' · '.join(body + (['来源：' + src] if src else []))

    diff = {}
    if new_title != raw_title:
        diff['title'] = new_title
    if new_note != (ev.get('note') or ''):
        diff['note'] = new_note
    return diff


# ---------------------------------------------------------------- 读写

def load(path):
    with open(path, encoding='utf-8') as f:
        html = f.read()
    m = BLOCK_RE.search(html)
    if not m:
        sys.exit('错误：%s 中没有 id="us-cal-data" 数据块' % path)
    data = json.loads(m.group(2), object_pairs_hook=collections.OrderedDict)
    data.setdefault('events', [])
    return html, m, data


def cmd_check(args):
    _h, _m, data = load(args.portal)
    evs = data['events']
    bad = []
    for e in evs:
        got = classify(e)
        key = (e.get('d'), e.get('title'))
        if not got.get('cat'):
            bad.append((key, '无法归类', got['_from']))
            continue
        if got['cat'] not in CAT_DEF:
            bad.append((key, '非法 cat=%r' % got['cat'], got['_from']))
        if got['topic'] not in TOPIC_DEF:
            bad.append((key, '非法 topic=%r' % got['topic'], got['_from']))
        if got['actor'] not in ACTOR_DEF:
            bad.append((key, '非法 actor=%r' % got['actor'], got['_from']))
        # 已写入的数据与规则结果不一致时提示（改过规则没重跑 apply）
        for f in ('cat', 'topic', 'actor'):
            if e.get(f) and e.get(f) != got[f]:
                bad.append((key, '%s 数据=%r 规则=%r' % (f, e.get(f), got[f]), got['_from']))

    cur = collections.Counter(e.get('cat') for e in evs)
    print('条目 %d 条，规则可归类 %d 条' % (len(evs), len(evs) - len([b for b in bad if b[1] == '无法归类'])))
    print('当前分布：' + ' · '.join('%s=%d' % (CAT_DEF.get(k, k), cur[k])
                                   for k in CAT_DEF if cur.get(k)))
    # 建议项：格内标签宽度与「对象 + 动作」写法（不拦发布，但会提示补写）
    import re as _re
    RE_DOING = _re.compile(r'生效|废止|失效|届满|到期|上调|提交|截止|开幕|计票|就职|审查|期限|上调')
    tips = []
    for e in evs:
        s0 = e.get('s') or ''
        w0 = sum(10.5 if ord(c) > 127 else 5.5 for c in s0)
        if w0 > 95:
            tips.append((e.get('d'), s0, '标签 %.0fpx，超过 95px 会被截断' % w0))
        elif _re.search(r'生效|废止|届满|期限', e.get('title') or '') and not RE_DOING.search(s0):
            tips.append((e.get('d'), s0, '规则类标签只写了名词，须补「生效/废止/届满」等动作'))
    if tips:
        print('\n[建议] %d 条标签可再明确：' % len(tips))
        for d, s0, msg in tips[:15]:
            print('  %s  %-16s %s' % (d, s0, msg))

    if bad:
        print('\n[未通过] %d 项问题：' % len(bad))
        for (d, t), msg, frm in bad[:30]:
            print('  %s %-40s %s（规则：%s）' % (d, (t or '')[:38], msg, frm))
        return 1
    print('\n[通过] 三项指标均合法且与规则一致。')
    return 0


def cmd_report(args):
    _h, _m, data = load(args.portal)
    evs = data['events']
    rows = []
    for e in evs:
        got = classify(e)
        rows.append(collections.OrderedDict([
            ('日期', e.get('d')),
            ('结束日', e.get('d2', '')),
            ('范畴', CAT_DEF.get(got['cat'], got['cat'])),
            ('议题', TOPIC_DEF.get(got['topic'], got['topic'])),
            ('主体', ACTOR_DEF.get(got['actor'], got['actor'])),
            ('标题', e.get('title')),
            ('短标签', e.get('s', '')),
            ('备注', e.get('note', '')),
        ]))
    if args.csv:
        with open(args.csv, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print('已写出 %s（%d 行）' % (args.csv, len(rows)))
        return 0

    by = collections.OrderedDict((k, []) for k in CAT_DEF)
    for r, e in zip(rows, evs):
        by[classify(e)['cat']].append(r)
    for cat, items in by.items():
        if not items:
            continue
        print('\n【%s】%s · %d 条' % (cat, CAT_DEF[cat], len(items)))
        for r in items[:8]:
            print('  %s  %-22s %s' % (r['日期'], r['标题'][:22], r['议题']))
        if len(items) > 8:
            print('  …… 其余 %d 条' % (len(items) - 8))
    return 0


def cmd_apply(args):
    html, m, data = load(args.portal)
    evs = data['events']
    n_cat = n_hol = 0
    changes = []
    for e in evs:
        got = classify(e)
        if not got.get('cat'):
            sys.exit('错误：无法归类 %s %s' % (e.get('d'), e.get('title')))
        touched = False
        for f in ('cat', 'topic', 'actor'):
            if e.get(f) != got[f]:
                e[f] = got[f]
                touched = True
        if e.pop('impact', None) is not None:
            touched = True
        if touched:
            n_cat += 1
            changes.append(('指标', e.get('d'), e.get('title'),
                            '%s/%s/%s' % (got['cat'], got['topic'], got['actor'])))
        if args.fix_holiday:
            d = fix_holiday(e)
            if d:
                n_hol += 1
                e.update(d)

    print('分类指标：%d 条中 %d 条更新' % (len(evs), n_cat))
    if args.fix_holiday:
        print('假日口径：%d 条更新' % n_hol)
    if args.verbose:
        for kind, d, t, info in changes:
            print('  %s %s %-38s %s' % (kind, d, (t or '')[:36], info))

    if args.dry_run:
        print('\n--dry-run：未写入 %s' % args.portal)
        return 0

    block = '\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    with open(args.portal, 'w', encoding='utf-8') as f:
        f.write(html[:m.start(2)] + block + html[m.end(2):])
    print('已写入 %s' % args.portal)
    return 0


def main():
    ap = argparse.ArgumentParser(description='大事日历分类与口径规范化')
    ap.add_argument('--portal', default=DEFAULT_PORTAL)
    sub = ap.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('check', help='体检：覆盖、取值、与规则一致性')
    c.set_defaults(func=cmd_check)

    r = sub.add_parser('report', help='按范畴打印分类结果，或导出 CSV')
    r.add_argument('--csv', help='导出 CSV 路径')
    r.set_defaults(func=cmd_report)

    a = sub.add_parser('apply', help='写入分类指标（幂等）')
    a.add_argument('--dry-run', action='store_true')
    a.add_argument('--fix-holiday', action='store_true', help='同时统一假日 title/note 口径')
    a.add_argument('--verbose', action='store_true')
    a.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
