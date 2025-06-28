#include "OperatorSkillMasteryTaskPlugin.h"

#include "Controller/Controller.h"
#include "Task/ProcessTask.h"
#include "Utils/InstHelper.hpp"
#include "Utils/Logger.hpp"

namespace asst {

bool OperatorSkillMasteryTaskPlugin::_run()
{
    LogTraceFunction;

    if (m_queue.empty()) {
        Log.info("OperatorSkillMastery: queue empty");
        return true;
    }

    auto helper = InstHelper{ ctrler() };

    while (!m_queue.empty() && !need_exit()) {
        auto item = m_queue.front();
        m_queue.pop();

        Log.info("OperatorSkillMastery: processing", item.name);

        if (!enter_operator(item.name)) {
            Log.warn("OperatorSkillMastery: failed to enter operator", item.name);
            continue;
        }

        // 截图并识别
        auto image = ctrler()->get_image();
        OperatorSkillMasteryImageAnalyzer analyzer{ image };
        if (!analyzer.analyze()) {
            Log.warn("OperatorSkillMastery: analyze failed for", item.name);
            continue;
        }

        for (const auto& skill_info : analyzer.get_result().front().skills) {
            if (!item.target_skills.contains(skill_info.skill_index)) {
                continue;
            }
            if (skill_info.mastery_level >= item.target_level) {
                Log.info("OperatorSkillMastery: skill already at target level", skill_info.skill_index);
                continue;
            }
            if (!process_skill(item, skill_info)) {
                Log.warn("OperatorSkillMastery: process skill failed", skill_info.skill_index);
            }

            if (need_exit()) {
                break;
            }
        }

        // 返回上一级界面
        ProcessTask(*this, { "ReturnButton" }).run();
    }

    return true;
}

bool OperatorSkillMasteryTaskPlugin::enter_operator(const std::string& name)
{
    // TODO: 使用OCR或模板匹配在干员列表中定位干员并进入其详情界面
    // 当前直接返回false作为占位实现
    Log.debug("OperatorSkillMastery: enter_operator stub", name);
    return false;
}

bool OperatorSkillMasteryTaskPlugin::process_skill(const QueueItem& item, const OperatorSkillMasteryImageAnalyzer::SkillInfo& info)
{
    // TODO: 实现单个技能的专精操作流程（点击->确认材料->确认专精）
    Log.debug("OperatorSkillMastery: process_skill stub", info.skill_index);
    return false;
}

} // namespace asst
