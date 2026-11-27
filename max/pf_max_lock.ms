-- 板式家具建模：3ds Max 占用标记（同一个 Max 同一时间只给一个会话用）
--
-- 标记存在 Max 内存的全局变量里：换场景（resetMaxFile／loadMaxFile）不清除，Max 退出或重启后自动消失，不会留下死锁。
-- 2026-10-24 实测：Claude 与 Codex 两个会话同时连同一个 Max，后接入的一方等待无响应、旧实例随后退出，浪费约 3 分钟。
--
-- 用法（经 MCP execute_maxscript 发送，路径用正斜杠）：
--   (fileIn "<技能目录>/scripts/pf_max_lock.ms"; ::PFMaxLock.acquire "Codex" "<任务目录>")
--   同一次调用里 fileIn 之后必须写 ::PFMaxLock（强制按全局查找），否则编译时还不认识它，会报 undefined（2026-10-24 实测）。
--   返回 "OK ..." 才能继续；返回 "BUSY ..." 时不要对这个 Max 做任何写操作，把占用者、任务和场景告诉用户。
--   阶段保存后：PFMaxLock.touch "Codex"      交付或中断前：PFMaxLock.release "Codex"
--   只查看：PFMaxLock.status()
-- 最后活动超过 PF_MAX_LOCK_IDLE_MIN 分钟的标记视为失效，可直接接管（返回里注明“接管失效标记”）。
(
    global PF_MAX_LOCK                 -- #(占用者, 任务, 开始时间, 最后活动秒数, 最后活动时间文字)；不在此赋值，重复 fileIn 不清除
    global PF_MAX_LOCK_FILE            -- 可选的锁状态文件路径（长渲染卡住主线程期间由执行器后台线程原子续期）
    global PF_MAX_LOCK_IDLE_MIN = 15
    global PFMaxLock

    struct PFMaxLockStruct (
        fn pfLockNow = (((dotNetClass "System.DateTime").Now.Ticks) / 10000000L),
        fn pfUnixNow = ((dotNetClass "System.DateTimeOffset").UtcNow.ToUnixTimeSeconds()),
        fn pfLockStamp = ((dotNetClass "System.DateTime").Now.ToString "yyyy-MM-dd HH:mm:ss"),
        fn pfLockScene = (if maxFileName == "" then "未保存的新场景" else maxFilePath + maxFileName),
        fn readFileLock = (
            if PF_MAX_LOCK_FILE == undefined or PF_MAX_LOCK_FILE == "" then return undefined
            try (
                local fcls = dotNetClass "System.IO.File"
                if not (fcls.Exists PF_MAX_LOCK_FILE) then return undefined
                local enc = (dotNetClass "System.Text.Encoding").UTF8
                local lines = fcls.ReadAllLines PF_MAX_LOCK_FILE enc
                local fOwner = ""
                local fTask = ""
                local fStarted = ""
                local fEpoch = undefined
                local fStamp = ""
                for line in lines do (
                    local idx = findString line "="
                    if idx != undefined then (
                        local k = substring line 1 (idx - 1)
                        local v = substring line (idx + 1) -1
                        if k == "owner" then fOwner = v
                        else if k == "task" then fTask = v
                        else if k == "started" then fStarted = v
                        else if k == "epoch" then (
                            try (fEpoch = (dotNetClass "System.Int64").Parse v) catch ()
                        )
                        else if k == "stamp" then fStamp = v
                    )
                )
                if fOwner == "" or fEpoch == undefined then return undefined
                #(fOwner, fTask, fStarted, fEpoch, fStamp)
            ) catch (undefined)
        ),
        fn writeFileLock owner task started unixSec stamp = (
            if PF_MAX_LOCK_FILE == undefined or PF_MAX_LOCK_FILE == "" then return ok
            try (
                local fcls = dotNetClass "System.IO.File"
                local dcls = dotNetClass "System.IO.Directory"
                local pcls = dotNetClass "System.IO.Path"
                local enc = dotNetObject "System.Text.UTF8Encoding" false
                local parent = pcls.GetDirectoryName PF_MAX_LOCK_FILE
                if parent != undefined and parent != "" and not (dcls.Exists parent) then dcls.CreateDirectory parent
                local body = "owner=" + owner + "\ntask=" + task + "\nstarted=" + started +
                             "\nepoch=" + (unixSec as string) + "\nstamp=" + stamp + "\n"
                local guid = (dotNetClass "System.Guid").NewGuid()
                local tmp = PF_MAX_LOCK_FILE + "." + (guid.ToString "N") + ".tmp"
                fcls.WriteAllText tmp body enc
                fcls.Copy tmp PF_MAX_LOCK_FILE true
                try (fcls.Delete tmp) catch ()
            ) catch ()
            ok
        ),
        fn clearFileLock owner force:false = (
            if PF_MAX_LOCK_FILE == undefined or PF_MAX_LOCK_FILE == "" then return ok
            try (
                local rec = readFileLock()
                if rec == undefined or rec[1] == owner or force then (
                    local fcls = dotNetClass "System.IO.File"
                    if fcls.Exists PF_MAX_LOCK_FILE then fcls.Delete PF_MAX_LOCK_FILE
                )
            ) catch ()
            ok
        ),
        fn idleMin = (
            if PF_MAX_LOCK == undefined then 0
            else (
                local memIdle = ((pfLockNow() - PF_MAX_LOCK[4]) as float) / 60.0
                local rec = readFileLock()
                if rec != undefined and rec[1] == PF_MAX_LOCK[1] then (
                    local fileIdle = ((pfUnixNow() - rec[4]) as float) / 60.0
                    if fileIdle < 0.0 then fileIdle = 0.0
                    if fileIdle < memIdle then (
                        PF_MAX_LOCK[4] = pfLockNow() - ((fileIdle * 60.0) as integer64)
                        if rec[5] != "" then PF_MAX_LOCK[5] = rec[5]
                        return fileIdle
                    )
                )
                memIdle
            )
        ),
        fn describe = (
            if PF_MAX_LOCK == undefined then "FREE 当前无人占用；场景 " + (pfLockScene())
            else (
                local idle = idleMin()
                "HELD 占用者=" + PF_MAX_LOCK[1] + " 任务=" + PF_MAX_LOCK[2] + " 开始=" + PF_MAX_LOCK[3] +
                " 最后活动=" + PF_MAX_LOCK[5] + "（" + (formattedPrint idle format:".1f") + " 分钟前） 场景=" + (pfLockScene())
            )
        ),
        fn status = (describe()),
        fn acquire owner task = (
            if PF_MAX_LOCK == undefined or PF_MAX_LOCK[1] == owner or idleMin() > PF_MAX_LOCK_IDLE_MIN then (
                local note = if PF_MAX_LOCK != undefined and PF_MAX_LOCK[1] != owner then "（接管失效标记：" + PF_MAX_LOCK[1] + "）" else ""
                local started = if PF_MAX_LOCK != undefined and PF_MAX_LOCK[1] == owner then PF_MAX_LOCK[3] else pfLockStamp()
                local stamp = pfLockStamp()
                PF_MAX_LOCK = #(owner, task, started, pfLockNow(), stamp)
                writeFileLock owner task started (pfUnixNow()) stamp
                "OK 已占用" + note + "：" + (describe())
            ) else "BUSY " + (describe())
        ),
        fn touch owner = (
            if PF_MAX_LOCK != undefined and PF_MAX_LOCK[1] == owner then (
                local stamp = pfLockStamp()
                PF_MAX_LOCK[4] = pfLockNow()
                PF_MAX_LOCK[5] = stamp
                writeFileLock owner PF_MAX_LOCK[2] PF_MAX_LOCK[3] (pfUnixNow()) stamp
                "OK 已刷新：" + (describe())
            ) else "NOT_OWNER " + (describe())
        ),
        fn release owner force:false = (
            if PF_MAX_LOCK == undefined then (
                clearFileLock owner force:force
                "OK 本来就无人占用"
            )
            else if PF_MAX_LOCK[1] == owner or force then (
                local who = PF_MAX_LOCK[1]
                PF_MAX_LOCK = undefined
                clearFileLock owner force:force
                "OK 已释放（" + who + "）"
            )
            else "NOT_OWNER " + (describe())
        )
    )
    PFMaxLock = PFMaxLockStruct()
    "PFMaxLock 已加载"
)
