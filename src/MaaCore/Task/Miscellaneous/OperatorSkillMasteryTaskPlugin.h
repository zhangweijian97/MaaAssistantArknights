#pragma once

#include "Task/AbstractTask.h"
#include "Vision/Miscellaneous/OperatorSkillMasteryImageAnalyzer.h"
#include <queue>
#include <string>
#include <unordered_set>

namespace asst {

/**
 * OperatorSkillMasteryTaskPlugin
 * 负责导航游戏 UI，批量执行干员技能专精操作。
 * 使用 OperatorSkillMasteryImageAnalyzer 进行界面识别。
 */
class OperatorSkillMasteryTaskPlugin : public AbstractTask {
public:
    using AbstractTask::AbstractTask;
    ~OperatorSkillMasteryTaskPlugin() override = default;

    struct QueueItem {
        std::string name; // 干员名称
        std::unordered_set<int> target_skills; // 需要专精的技能序号
        int target_level = 3; // 目标专精等级
    };

    /**
     * 将待专精干员加入队列。
     */
    void enqueue(const QueueItem& item) { m_queue.push(item); }

protected:
    bool _run() override;

private:
    bool enter_operator(const std::string& name);
    bool process_skill(const QueueItem& item, const OperatorSkillMasteryImageAnalyzer::SkillInfo& info);

    std::queue<QueueItem> m_queue; // 专精队列
};

} // namespace asst
