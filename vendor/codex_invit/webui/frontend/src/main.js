import { createApp } from 'vue'
import { createPinia } from 'pinia'

// 保留全量 CSS（保证 ElMessage/ElMessageBox 等程序式组件样式），JS 由 unplugin 按需 tree-shake
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/theme.css'
import 'nprogress/nprogress.css'

// 只注册实际用到的图标（动态 <component :is="name"> 需要全局注册）
import {
  Platform, Fold, Expand, Moon, Sunny, User, ArrowDown,
  Odometer, Upload, VideoPlay, Connection, Files,
  CircleCheck, Document, Message, Iphone, Share,
  Loading, Select, CircleClose, Refresh, RefreshRight, CopyDocument,
  Bell, Close, Download,
  // codex_invit 新增（卡密 / 接码价格 / 邀请与执行）
  Ticket, TrendCharts, Coin, Timer, Key, DataLine, Setting,
  Warning, InfoFilled, Monitor, Operation, Cpu,
} from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'

const ICONS = {
  Platform, Fold, Expand, Moon, Sunny, User, ArrowDown,
  Odometer, Upload, VideoPlay, Connection, Files,
  CircleCheck, Document, Message, Iphone, Share,
  Loading, Select, CircleClose, Refresh, RefreshRight, CopyDocument,
  Bell, Close, Download,
  Ticket, TrendCharts, Coin, Timer, Key, DataLine, Setting,
  Warning, InfoFilled, Monitor, Operation, Cpu,
}

const app = createApp(App)
for (const [name, comp] of Object.entries(ICONS)) app.component(name, comp)

app.use(createPinia())
app.use(router)
app.mount('#app')
