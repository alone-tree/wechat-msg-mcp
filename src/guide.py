"""使用说明书：MCP instructions 只放短引导，完整说明由 system(action=start) 返回。

平台无关：不依赖 DSH 的 mcp_load，任何 MCP 客户端调 system 工具都能拿到全文。
改说明只改本文件，两处同时生效。
"""

INSTRUCTIONS = """微信聊天记录只读查询（本机数据，不联网）。

开始前先调 system(action="start")，它返回完整使用说明与当前数据状态。

六个查询工具：list_entities 列会话与联系人，get_messages 读消息，relations 查关系，
search 搜索，list_moments 朋友圈，list_favorites 收藏。

消息不带收发方向：from 为「我」就是本人发的。默认 detail=lite，要 ID 与类型传 detail="full"。

查询前会自动按 TTL（默认 60 秒）增量刷新，不需要预热脚本；要立刻拿最新数据调 system(action="sync")。
"""

GUIDE = """微信聊天记录只读查询（本机数据，不联网）。七个工具，其中 system 是入口。

【账号是谁】
- system(action="start") 与 get_messages 返回里都有 self：id 是账号 wxid，nickname 是微信昵称，alias 是微信号。
- 每条消息只有 from：本人发的固定「我」，别人发的是备注优先的显示名，系统消息的 from 为空。要看 wxid 去 senders 表（detail=full 时才有）按名字反查。

【消息长什么样】
lite 档（默认）：id、time、from、text，非纯文本时多一个 media。跨会话查询时每行还带 chat。
full 档：再加 sender_id、type、subtype、chat_id、chat_name，以及 senders 表。
raw 档：再加 local_type、raw（原始 XML）与 db，仅供排查。

text 是自足的，单看这一行就能读懂，不用再拼类型：
- 文字：原文。图片：「[图片]」。表情：「[表情]」。
- 语音：转写全文，没有转写时「[语音 12 秒]」。视频：「[视频 12 秒]」。
- 文件：「[文件] 简历.pdf」。链接：标题。转账：「[转账] 金额」。红包：「[红包]」。
- 位置：「[位置] 地名」。通话：「[通话] 时长」。应用消息：「[小程序] 标题」等。
- 系统消息：「[系统] 某某 撤回了一条消息」这类一句话，原文只在 raw 档。

引用消息的 text 是回复正文，被引内容在 quote{from, text}。

【群名】
有备注用备注，有群昵称用群昵称，都没有时用成员昵称拼（如「甲、乙、丙等8人」），
name_source 标明来源：remark / nickname / members / fallback。拼出来的名字只用于辨认，
要精确找群用 id。四个来源都取不到时名字是「群聊」。

【分页】
固定四件套：total（命中总数）、returned（本次条数）、has_more、next。
消息的 next 是游标字符串，原样传回 cursor 取下一页；其余工具的 next 是 offset 数值，传回 offset。
多会话查询时 per_target_limit 控制每个会话最多取多少条，0 表示不单独限制；
被截断的会话会在 warnings 里点名，不会静默丢数据。

【默认行为】
- 调任何查询工具时，服务会按 TTL（默认 60 秒）增量刷新解密数据，不需要预热脚本，也不需要重启服务。
- refresh 说明本次做了什么：refreshed=true 表示刚解了 changed_dbs 里的库；skipped=ttl 表示 TTL 内直接用现有数据；locked 表示另一个进程在刷。
- data_until 是该数据源最后一次写入的时间，不是最新消息时间。消息看 message 库，朋友圈看 sns，收藏看 favorite，会话看 session 与 contact。
- 要绕过 TTL 立刻拉最新时调 system(action="sync")。
- refresh.failed_detail 是解密失败原因；refresh.shadow 表示正本被别的进程占用、已写旁路副本，查到的仍是最新数据。

【七个工具】
- system：系统入口。action=start 返回本说明书与当前数据状态；status 只看数据新鲜度；sync 立刻刷新数据与索引。
- list_entities：列会话 / 联系人 / 群。scope=session|contact|group|all。给名称、备注、微信号、地区、性别、好友关系、成员数、群主、群公告、未读数、最后活跃时间、name_source。
- get_messages：读消息。targets 可混传人与群；sender 限定发言人；types 限定类型；时间用 start_date / end_date。默认最新优先，数组第一个最新。
- relations：group_members 群成员；common_groups 某人所在的群；person_activity 某人在多群的发言；activity 单会话统计。
- search：一次搜实体与消息，消息带命中片段，offset 翻页。
- list_moments：朋友圈动态，含图片链接、点赞、评论。
- list_favorites：收藏条目，含类型、链接、标签。

【types 取值】
text image voice video emoticon file link mini_program quote chat_record channels
transfer red_packet location card voip system revoked。
写错取值会直接报错并给出 candidates，不会静默返回 0 条。

【读媒体】
媒体默认就处理（include_media 默认开）。返回条数超过 50 条时自动跳过媒体提取，因为那种量级会被截成预览。
- 图片、表情：直接内联图片本身，能直接看到图，不用再读文件。
- 视频：内联封面图本身；本地有 mp4 时 media.path 指向原件，另给 size 与 exists；要留封面文件用 extract_media。
- 语音：给转写文字并写进 text，不内联音频；要音频传 extract_media。
- 链接、小程序、视频号：media.url 可直接打开。转账给 media.amount。红包金额本地不存。
- 文件：自动复制到导出目录，media.path 是副本路径可直接读，source_path 是微信原路径。

媒体处理比纯文本慢：批量取数不要开 include_media，要看图时配合 types 单独查（如 types=["image"]）。
内联图片总字节超过 6MB 时不内联，改成落盘并给 media.path，media_inline.note 说明还有多少张。
图片定位三级兜底：message_resource 落盘基名、hardlink 登记表、按消息自带长度与宽高认文件。

拿不到媒体时 media 一定带 missing_reason，别猜：no_local_image（原图和缩略图都不在本机，
这张图从没在微信里打开过，让用户点开一次）、no_local_file、no_image_key、decode_failed、
voice_model_missing、no_audio、too_many_rows、not_requested、budget_exhausted。顶层 media_missing 给各类张数。
只有缩略图时 media.source=thumb，media.note 会说明原图未落盘。长截图（长宽比超过 3）切成多段返回，
media.tiles 是段数，段间约 80 像素重叠，边界重复的一两行属正常；整图在 media.path。

【结果很大时】
- 超过 200 行或 80KB 自动落盘，只留前 3 条预览，auto_export 给路径与 is_partial。
- 强制落盘用 output="file"：会按游标翻页取满再写，不只写当前页；is_partial=false 说明全量。
- format 可选 csv / json / ndjson / md / text / html。md 与 text 按会话分节、一行一条，适合直接给人看。
- 落盘目录默认本进程的 exports/run-<pid>-<时间>/，12 小时后自动清理；指定位置用 output_dir。
- 拿到路径后用文件读取工具去读，不要把几千条塞进对话。

【复杂查询怎么办】
本工具只做常用查询，不做复杂聚合，复杂的事交给脚本调 CLI。
一、多次调用自己拼：先 list_entities / relations 拿范围，再 get_messages 分批取，自己聚合。
二、写脚本：项目提供与 MCP 同参的 CLI，默认输出 NDJSON（一行一条），最适合管道与循环：
  python -m src.cli list / messages / rel / search / groups / moments / favorites / export / report / status / sync / index
  例：python -m src.cli messages --targets 某群 --start 2026-11-01 --limit 500
  例：python -m src.cli groups --members 甲 乙 --json        # 按成员组合找群
  例：python -m src.cli messages --targets 某群 --format text  # 人读分节文本
  Python：from src.service import Service; svc = Service(); svc.get_messages(targets=[...], limit=500)
Service 默认也按 TTL 刷新（auto_refresh=True），显式传 False 才关。

【几个容易踩的点】
- data_until 不是最新消息时间，是数据源最后写入时间，差几分钟属正常。
- 微信还没落盘的 WAL 内容谁都读不到，刚发的消息可能差一步，等几秒再试。
- 群里的非好友只有名字：没缓存过资料卡的人查不到微信号、地区、性别，这是微信本身的行为。
- 群管理员、群昵称、@提及都不在本地库里，标不了；群主在 chat_room.owner。
- 拼出来的群名里可能和客户端有细微差别（客户端用群昵称，本地只有备注与昵称）。
- 朋友圈与收藏有自己的关键词参数，不进 search 的全库索引。
- 语音识别依赖本地模型，模型缺失时结果里有 warnings 告诉模型该放哪。
"""
