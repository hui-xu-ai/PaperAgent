/* PaperAgent 前端版本（**单一来源**，见 docs/VERSIONING.md §1）。
 *
 * 发布时与 backend/app/version.py::APP_VERSION 保持一致：
 *   - backend/tests/test_version_contract.py 断言两者相等（不一致即 CI 失败）；
 *   - 后端 /api/version 读的正是**本文件**（随包发出的那份），因此"前后端打歪"能被发现；
 *   - GUI「关于」同时显示前端版本与后端版本，不一致会标红提示。
 */
window.PAPERAGENT_FRONTEND_VERSION = "1.2.0";
