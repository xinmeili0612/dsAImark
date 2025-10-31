import os
import time
import schedule
from openai import OpenAI
import ccxt
import pandas as pd
from datetime import datetime
import json
import re
from dotenv import load_dotenv

load_dotenv()

# 初始化DeepSeek客户端
deepseek_client = OpenAI(
    api_key=os.getenv('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com"
)

# 初始化OKX交易所
exchange = ccxt.okx({
    'apiKey': os.getenv('OKX_API_KEY'),
    'secret': os.getenv('OKX_SECRET'),
    'password': os.getenv('OKX_PASSWORD'),  # OKX需要交易密码
    'options': {
        'defaultType': 'swap',  # OKX使用swap表示永续合约
    },
})

# 交易参数配置 - AI动态杠杆版本（适配100USDT本金）
TRADE_CONFIG = {
    'symbol': 'BTC/USDT:USDT',  # OKX的合约符号格式
    'timeframe': '15m',  # 使用15分钟K线
    'test_mode': False,  # 测试模式
    'data_points': 96,  # 24小时数据（96根15分钟K线）
    # 账户/交易模式
    'td_mode': 'cross',           # 订单交易模式：'cross' 或 'isolated'
    'hedge_mode': True,           # 是否启用双向持仓（多空同时）
    'analysis_periods': {
        'short_term': 20,  # 短期均线
        'medium_term': 50,  # 中期均线
        'long_term': 96  # 长期趋势
    },
    # AI智能仓位管理（利益最大化优化版本）
    'position_management': {
        'enable_intelligent_position': True,  # 启用智能仓位
        'base_usdt_amount': 70,  # 基础USDT投入（100U本金，提高到70%）
        'high_confidence_multiplier': 1.3,  # 高信心时91 USDT（约90%本金）
        'medium_confidence_multiplier': 1.0,  # 中信心时70 USDT（70%本金）
        'low_confidence_multiplier': 0.6,  # 低信心时42 USDT（保守模式）
        'max_position_ratio': 0.9,  # 最多使用90%账户余额（利益最大化）
        'trend_strength_multiplier': 1.5,  # 强势趋势时增加50%（可达到105%但限制在90%）
        'enable_pyramid': True,  # 启用金字塔加仓
        'pyramid_threshold': 0.05,  # 浮盈5%时考虑加仓
        'pyramid_amount_ratio': 0.3,  # 加仓金额为原仓位的30%
        'max_pyramid_times': 2  # 最多加仓2次
    },
    # AI动态杠杆配置（保守平衡版 - 收益与风险平衡）
    'dynamic_leverage': {
        'enable_dynamic_leverage': True,  # 启用AI动态杠杆
        'leverage_ranges': {
            'HIGH': [6, 8],      # 高信心：6-8倍杠杆（保守上限，降低风险）
            'MEDIUM': [4, 6],    # 中信心：4-6倍杠杆
            'LOW': [2, 3]        # 低信心：2-3倍杠杆
        },
        'volatility_adjustment': {
            'low_volatility': 1.15,   # 低波动时+15%杠杆（保守调整）
            'high_volatility': 0.85   # 高波动时-15%杠杆（保守调整）
        },
        'rsi_adjustment': {
            'oversold': 1.1,      # RSI<30时+10%杠杆（保守调整）
            'overbought': 0.9,    # RSI>70时-10%杠杆（保守调整）
            'neutral': 1.0        # RSI中性时不变
        },
        'max_leverage': 8,       # 最大杠杆限制：8倍（安全上限，爆仓阈值12.5%）
        'min_leverage': 2         # 最小杠杆限制：2倍（更保守）
    },
    # 动态风险收益比配置
    'risk_reward': {
        'enable_dynamic_rr': True,  # 启用动态风险收益比
        'trend_bullish': 5,    # 强势上涨趋势：1:5
        'trend_bearish': 5,    # 强势下跌趋势：1:5
        'trend_consolidation': 1.5,  # 震荡整理：1:1.5
        'default': 3  # 默认：1:3
    },
    # 移动止损配置
    'trailing_stop': {
        'enable_trailing_stop': True,  # 启用移动止损
        'breakeven_threshold': 0.05,  # 浮盈5%时移到成本价
        'lock_profit_1_threshold': 0.10,  # 浮盈10%时锁定3%利润
        'lock_profit_1_level': 0.03,
        'lock_profit_2_threshold': 0.20,  # 浮盈20%时锁定10%利润
        'lock_profit_2_level': 0.10,
        'update_interval': 1  # 每1根K线检查一次
    },
    # 分批止盈配置
    'partial_take_profit': {
        'enable_partial_tp': True,  # 启用分批止盈
        'tp1_ratio': 0.3,  # 30%仓位在1.5倍风险收益比止盈
        'tp1_rr_multiplier': 1.5,
        'tp2_ratio': 0.3,  # 30%仓位在2.5倍风险收益比止盈
        'tp2_rr_multiplier': 2.5,
        'tp3_ratio': 0.4   # 40%仓位跟随趋势到反转信号
    }
}

# 交易节流与频次控制配置（可按波动分档自适应）
TRADE_THROTTLE = {
    'low_bb_width': 0.02,     # 低波动阈值（布林带宽占比）
    'high_bb_width': 0.05,    # 高波动阈值
    'low_atr_ratio': 0.015,   # 低波动阈值（ATR/Price）
    'high_atr_ratio': 0.03,   # 高波动阈值

    # 各分档参数（可回测微调）
    'low':   {'persist': 3, 'cooldown': 6, 'min_move_atr': 1.0, 'max_trades_day': 2},
    'mid':   {'persist': 2, 'cooldown': 4, 'min_move_atr': 0.8, 'max_trades_day': 5},
    'high':  {'persist': 1, 'cooldown': 3, 'min_move_atr': 1.2, 'max_trades_day': 6},

    # 杠杆/价格变化阈值
    'leverage_tol': 0.5,  # 杠杆变化小于该值时不重新设置
}

# 最近交易信息（节流用）
last_trade_info = {
    'timestamp': None,
    'bar_index': None,
    'side': None,
    'price': None,
    'count_today': 0,
    'date': None,
}

# 全局变量存储历史数据
price_history = []
signal_history = []
position = None

# 持仓管理全局变量（用于移动止损和加仓）
position_management = {
    'current_stop_loss': None,  # 当前止损价格
    'initial_stop_loss': None,  # 初始止损价格
    'entry_price': None,  # 开仓价格
    'pyramid_count': 0,  # 加仓次数
    'partial_tp_executed': {  # 分批止盈执行状态
        'tp1': False,
        'tp2': False,
        'tp3': False
    },
    'last_trailing_check': None  # 上次移动止损检查的时间
}


def cleanup_stop_loss_orders():
    """清理所有止盈止损订单"""
    try:
        print("🔧 检查并清理现有止盈止损订单...")
        open_orders = exchange.fetch_open_orders(TRADE_CONFIG['symbol'])
        
        cancelled_orders = []
        for order in open_orders:
            order_type = order.get('type', '')
            order_id = order.get('id', '')
            
            # 检查是否是止盈止损相关订单
            if order_type in ['stop_market', 'take_profit_market', 'conditional', 'trigger']:
                try:
                    exchange.cancel_order(order_id, TRADE_CONFIG['symbol'])
                    cancelled_orders.append(order_id)
                    print(f"✅ 已取消订单: {order_id} ({order_type})")
                except Exception as cancel_e:
                    print(f"⚠️ 取消订单失败: {order_id} - {cancel_e}")
        
        if cancelled_orders:
            print(f"📋 已清理 {len(cancelled_orders)} 个止盈止损订单")
            time.sleep(2)  # 等待订单取消完成
            return True
        else:
            print("📋 当前无止盈止损订单需要清理")
            return True
            
    except Exception as cleanup_e:
        print(f"⚠️ 订单清理过程出错: {cleanup_e}")
        return False


def safe_set_leverage(leverage, symbol, mgn_mode='cross'):
    """更安全的杠杆设置：不强制清理止盈止损，仅在必要时设置"""
    try:
        # 设置杠杆
        print(f"🔧 设置杠杆: {leverage}倍...")
        exchange.set_leverage(
            leverage,
            symbol,
            {'mgnMode': mgn_mode}
        )
        print(f"✅ 杠杆设置成功: {leverage}倍")
        return True
        
    except Exception as leverage_e:
        print(f"⚠️ 杠杆设置失败: {leverage_e}")
        try:
            # 兼容旧版ccxt签名
            exchange.set_leverage(leverage, symbol)
            print(f"✅ 杠杆设置成功（兼容模式）: {leverage}倍")
            return True
        except Exception as legacy_e:
            print(f"❌ 杠杆设置完全失败: {legacy_e}")
            return False


def setup_exchange():
    """设置交易所参数"""
    try:
        # 首先获取合约规格信息
        print("🔍 获取BTC合约规格...")
        markets = exchange.load_markets()
        btc_market = markets[TRADE_CONFIG['symbol']]
        
        # 获取合约乘数
        contract_size = float(btc_market['contractSize'])
        print(f"✅ 合约规格: 1张 = {contract_size} BTC")
        
        # 存储合约规格到全局配置
        TRADE_CONFIG['contract_size'] = contract_size
        TRADE_CONFIG['min_amount'] = btc_market['limits']['amount']['min']
        
        print(f"📏 最小交易量: {TRADE_CONFIG['min_amount']} 张")
        
        # 设置账户模式：双向持仓 + 保证金模式
        try:
            if TRADE_CONFIG.get('hedge_mode', True):
                exchange.set_position_mode(True)  # 启用双向持仓
                print("✅ 已启用双向持仓模式 (long/short)")
            else:
                exchange.set_position_mode(False)
                print("✅ 已启用单向持仓模式")
        except Exception as e:
            print(f"⚠️ 设置持仓模式失败: {e}")

        try:
            exchange.set_margin_mode(TRADE_CONFIG.get('td_mode', 'cross'), TRADE_CONFIG['symbol'])
            print(f"✅ 已设置保证金模式: {TRADE_CONFIG.get('td_mode', 'cross')}")
        except Exception as e:
            print(f"⚠️ 设置保证金模式失败: {e}")

        # OKX设置杠杆（使用默认5倍作为初始杠杆）
        initial_leverage = 5
        
        # 🔧 使用安全杠杆设置函数
        leverage_success = safe_set_leverage(
            initial_leverage, 
            TRADE_CONFIG['symbol'], 
            TRADE_CONFIG.get('td_mode', 'cross')
        )
        
        if not leverage_success:
            print("⚠️ 将使用默认杠杆进行交易")

        # 获取余额
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        print(f"当前USDT余额: {usdt_balance:.2f}")

        return True
    except Exception as e:
        print(f"交易所设置失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def calculate_dynamic_leverage(signal_data, price_data):
    """AI动态杠杆计算函数"""
    config = TRADE_CONFIG['dynamic_leverage']
    
    # 如果禁用动态杠杆，使用固定杠杆
    if not config.get('enable_dynamic_leverage', True):
        return 5  # 默认5倍杠杆
    
    try:
        # 1. 根据信号信心确定基础杠杆范围
        confidence = signal_data.get('confidence', 'MEDIUM')
        leverage_range = config['leverage_ranges'].get(confidence, [4, 6])
        base_leverage = (leverage_range[0] + leverage_range[1]) / 2  # 取中值
        
        print(f"📊 基础杠杆计算:")
        print(f"   - 信号信心: {confidence}")
        print(f"   - 杠杆范围: {leverage_range[0]}-{leverage_range[1]}倍")
        print(f"   - 基础杠杆: {base_leverage:.1f}倍")
        
        # 2. 根据市场波动性调整
        volatility_multiplier = 1.0
        if 'technical_data' in price_data:
            # 使用布林带宽度判断波动性
            bb_upper = price_data['technical_data'].get('bb_upper', 0)
            bb_lower = price_data['technical_data'].get('bb_lower', 0)
            bb_width = (bb_upper - bb_lower) / price_data['price'] if price_data['price'] > 0 else 0
            
            if bb_width < 0.02:  # 低波动
                volatility_multiplier = config['volatility_adjustment']['low_volatility']
                print(f"   - 波动性: 低 (BB宽度: {bb_width:.3f})")
            elif bb_width > 0.05:  # 高波动
                volatility_multiplier = config['volatility_adjustment']['high_volatility']
                print(f"   - 波动性: 高 (BB宽度: {bb_width:.3f})")
            else:
                print(f"   - 波动性: 中等 (BB宽度: {bb_width:.3f})")
        
        # 3. 根据RSI状态调整
        rsi_multiplier = 1.0
        if 'technical_data' in price_data:
            rsi = price_data['technical_data'].get('rsi', 50)
            if rsi < 30:
                rsi_multiplier = config['rsi_adjustment']['oversold']
                print(f"   - RSI状态: 超卖 ({rsi:.1f})")
            elif rsi > 70:
                rsi_multiplier = config['rsi_adjustment']['overbought']
                print(f"   - RSI状态: 超买 ({rsi:.1f})")
            else:
                print(f"   - RSI状态: 中性 ({rsi:.1f})")
        
        # 4. 计算最终杠杆
        final_leverage = base_leverage * volatility_multiplier * rsi_multiplier
        
        # 5. 应用杠杆限制
        max_leverage = config['max_leverage']
        min_leverage = config['min_leverage']
        final_leverage = max(min_leverage, min(max_leverage, final_leverage))
        
        print(f"📈 杠杆调整详情:")
        print(f"   - 波动性倍数: {volatility_multiplier}")
        print(f"   - RSI倍数: {rsi_multiplier}")
        print(f"   - 调整后杠杆: {final_leverage:.1f}倍")
        print(f"   - 最终杠杆: {final_leverage:.1f}倍 (限制: {min_leverage}-{max_leverage}倍)")
        
        return round(final_leverage, 1)
        
    except Exception as e:
        print(f"❌ 动态杠杆计算失败，使用默认杠杆: {e}")
        import traceback
        traceback.print_exc()
        return 5  # 默认5倍杠杆


def calculate_intelligent_position(signal_data, price_data, current_position):
    """计算智能仓位大小 - 基于USDT投入 + AI动态杠杆"""
    config = TRADE_CONFIG['position_management']
    
    # 如果禁用智能仓位，使用固定仓位
    if not config.get('enable_intelligent_position', True):
        fixed_contracts = 0.01  # 固定仓位大小
        print(f"🔧 智能仓位已禁用，使用固定仓位: {fixed_contracts} 张")
        return fixed_contracts, 5  # 返回固定杠杆
    
    try:
        # 🆕 1. 首先计算动态杠杆
        dynamic_leverage = calculate_dynamic_leverage(signal_data, price_data)
        
        # 获取账户余额
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        
        # 基础USDT投入
        base_usdt = config['base_usdt_amount']
        print(f"💰 可用USDT余额: {usdt_balance:.2f}, 基础投入{base_usdt} USDT")
        
        # 根据信心程度调整
        confidence_multiplier = {
            'HIGH': config['high_confidence_multiplier'],
            'MEDIUM': config['medium_confidence_multiplier'],
            'LOW': config['low_confidence_multiplier']
        }.get(signal_data.get('confidence', 'MEDIUM'), 1.0)
        
        # 根据趋势强度调整
        trend = price_data.get('trend_analysis', {}).get('overall', '震荡整理')
        if trend in ['强势上涨', '强势下跌']:
            trend_multiplier = config['trend_strength_multiplier']
        else:
            trend_multiplier = 1.0
        
        # 根据RSI状态调整（超买超卖区域减仓）
        rsi = price_data.get('technical_data', {}).get('rsi', 50)
        if rsi > 75 or rsi < 25:
            rsi_multiplier = 0.7
        else:
            rsi_multiplier = 1.0
        
        # 计算建议投入USDT金额
        suggested_usdt = base_usdt * confidence_multiplier * trend_multiplier * rsi_multiplier
        
        # 风险管理：不超过总资金的指定比例
        max_usdt = usdt_balance * config['max_position_ratio']
        final_usdt = min(suggested_usdt, max_usdt)
        
        # 🆕 使用动态杠杆计算合约张数
        # 公式：合约张数 = (投入USDT * 动态杠杆) / (当前价格 * 合约乘数)
        contract_size = (final_usdt * dynamic_leverage) / (price_data['price'] * TRADE_CONFIG['contract_size'])
        
        print(f"📊 仓位计算详情:")
        print(f"   - 基础USDT: {base_usdt}")
        print(f"   - 信心倍数: {confidence_multiplier}")
        print(f"   - 趋势倍数: {trend_multiplier}")
        print(f"   - RSI倍数: {rsi_multiplier}")
        print(f"   - 建议USDT: {suggested_usdt:.2f}")
        print(f"   - 最终USDT: {final_usdt:.2f}")
        print(f"   - 动态杠杆: {dynamic_leverage}倍")
        print(f"   - 计算合约: {contract_size:.4f} 张")
        
        # 精度处理：OKX BTC合约最小交易单位为0.01张
        contract_size = round(contract_size, 2)  # 保留2位小数
        
        # 确保最小交易量
        min_contracts = TRADE_CONFIG.get('min_amount', 0.01)
        if contract_size < min_contracts:
            contract_size = min_contracts
            print(f"⚠️ 仓位小于最小值，调整为: {contract_size} 张")
        
        print(f"🎯 最终仓位: {final_usdt:.2f} USDT → {contract_size:.2f} 张合约 (杠杆: {dynamic_leverage}倍)")
        return contract_size, dynamic_leverage
        
    except Exception as e:
        print(f"❌ 仓位计算失败，使用固定仓位: {e}")
        import traceback
        traceback.print_exc()
        # 紧急备用计算
        base_usdt = config['base_usdt_amount']
        contract_size = (base_usdt * 5) / (  # 使用默认5倍杠杆
                    price_data['price'] * TRADE_CONFIG.get('contract_size', 0.001))
        return round(max(contract_size, TRADE_CONFIG.get('min_amount', 0.01)), 2), 5


def calculate_technical_indicators(df):
    """计算技术指标 - 来自第一个策略"""
    try:
        # 移动平均线
        df['sma_5'] = df['close'].rolling(window=5, min_periods=1).mean()
        df['sma_20'] = df['close'].rolling(window=20, min_periods=1).mean()
        df['sma_50'] = df['close'].rolling(window=50, min_periods=1).mean()

        # 指数移动平均线
        df['ema_12'] = df['close'].ewm(span=12).mean()
        df['ema_26'] = df['close'].ewm(span=26).mean()
        df['macd'] = df['ema_12'] - df['ema_26']
        df['macd_signal'] = df['macd'].ewm(span=9).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']

        # 相对强弱指数 (RSI)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # 布林带
        df['bb_middle'] = df['close'].rolling(20).mean()
        bb_std = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
        df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
        df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        # 真实波动范围与ATR(20)
        high_low = df['high'] - df['low']
        high_close_prev = (df['high'] - df['close'].shift(1)).abs()
        low_close_prev = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
        df['atr_20'] = tr.rolling(window=20, min_periods=1).mean()
        df['atr_ratio'] = df['atr_20'] / df['close']

        # 成交量均线
        df['volume_ma'] = df['volume'].rolling(20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_ma']

        # 支撑阻力位
        df['resistance'] = df['high'].rolling(20).max()
        df['support'] = df['low'].rolling(20).min()

        # 填充NaN值
        df = df.bfill().ffill()

        return df
    except Exception as e:
        print(f"技术指标计算失败: {e}")
        return df


def get_support_resistance_levels(df, lookback=20):
    """计算支撑阻力位"""
    try:
        recent_high = df['high'].tail(lookback).max()
        recent_low = df['low'].tail(lookback).min()
        current_price = df['close'].iloc[-1]

        resistance_level = recent_high
        support_level = recent_low

        # 动态支撑阻力（基于布林带）
        bb_upper = df['bb_upper'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]

        return {
            'static_resistance': resistance_level,
            'static_support': support_level,
            'dynamic_resistance': bb_upper,
            'dynamic_support': bb_lower,
            'price_vs_resistance': ((resistance_level - current_price) / current_price) * 100,
            'price_vs_support': ((current_price - support_level) / support_level) * 100
        }
    except Exception as e:
        print(f"支撑阻力计算失败: {e}")
        return {}


def get_market_trend(df):
    """判断市场趋势"""
    try:
        current_price = df['close'].iloc[-1]

        # 多时间框架趋势分析
        trend_short = "上涨" if current_price > df['sma_20'].iloc[-1] else "下跌"
        trend_medium = "上涨" if current_price > df['sma_50'].iloc[-1] else "下跌"

        # MACD趋势
        macd_trend = "bullish" if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1] else "bearish"

        # 综合趋势判断
        if trend_short == "上涨" and trend_medium == "上涨":
            overall_trend = "强势上涨"
        elif trend_short == "下跌" and trend_medium == "下跌":
            overall_trend = "强势下跌"
        else:
            overall_trend = "震荡整理"

        return {
            'short_term': trend_short,
            'medium_term': trend_medium,
            'macd': macd_trend,
            'overall': overall_trend,
            'rsi_level': df['rsi'].iloc[-1]
        }
    except Exception as e:
        print(f"趋势分析失败: {e}")
        return {}


def get_btc_ohlcv_enhanced():
    """增强版：获取BTC K线数据并计算技术指标"""
    try:
        print(f"🔍 正在获取 {TRADE_CONFIG['symbol']} 的K线数据...")
        # 获取K线数据
        ohlcv = exchange.fetch_ohlcv(TRADE_CONFIG['symbol'], TRADE_CONFIG['timeframe'],
                                     limit=TRADE_CONFIG['data_points'])
        
        if not ohlcv or len(ohlcv) == 0:
            print("❌ 获取K线数据为空")
            return None
            
        print(f"✅ 成功获取 {len(ohlcv)} 根K线数据")

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        print(f"📊 DataFrame形状: {df.shape}")
        print(f"📊 最新价格: {df['close'].iloc[-1]:.2f}")

        # 计算技术指标
        print("🔧 正在计算技术指标...")
        df = calculate_technical_indicators(df)

        current_data = df.iloc[-1]
        previous_data = df.iloc[-2]

        # 获取技术分析数据
        print("📈 正在分析市场趋势...")
        trend_analysis = get_market_trend(df)
        if not trend_analysis:
            trend_analysis = {}
            
        print("🎯 正在计算支撑阻力位...")
        levels_analysis = get_support_resistance_levels(df)
        if not levels_analysis:
            levels_analysis = {}
        
        print("✅ 技术分析完成")
        return {
            'price': current_data['close'],
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'high': current_data['high'],
            'low': current_data['low'],
            'volume': current_data['volume'],
            'timeframe': TRADE_CONFIG['timeframe'],
            'price_change': ((current_data['close'] - previous_data['close']) / previous_data['close']) * 100,
            'kline_data': df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].tail(10).to_dict('records'),
            'technical_data': {
                'sma_5': current_data.get('sma_5', 0),
                'sma_20': current_data.get('sma_20', 0),
                'sma_50': current_data.get('sma_50', 0),
                'rsi': current_data.get('rsi', 0),
                'macd': current_data.get('macd', 0),
                'macd_signal': current_data.get('macd_signal', 0),
                'macd_histogram': current_data.get('macd_histogram', 0),
                'bb_upper': current_data.get('bb_upper', 0),
                'bb_lower': current_data.get('bb_lower', 0),
                'bb_position': current_data.get('bb_position', 0),
                'volume_ratio': current_data.get('volume_ratio', 0),
                'atr_20': current_data.get('atr_20', 0),
                'atr_ratio': current_data.get('atr_ratio', 0)
            },
            'trend_analysis': trend_analysis,
            'levels_analysis': levels_analysis,
            'full_data': df
        }
    except Exception as e:
        print(f"获取增强K线数据失败: {e}")
        return None


def generate_technical_analysis_text(price_data):
    """生成技术分析文本"""
    if not price_data or 'technical_data' not in price_data:
        return "技术指标数据不可用"

    tech = price_data['technical_data']
    trend = price_data.get('trend_analysis', {})
    levels = price_data.get('levels_analysis', {})

    # 检查数据有效性
    def safe_float(value, default=0):
        return float(value) if value and pd.notna(value) else default

    analysis_text = f"""
    【技术指标分析】
    📈 移动平均线:
    - 5周期: {safe_float(tech['sma_5']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_5'])) / safe_float(tech['sma_5']) * 100:+.2f}%
    - 20周期: {safe_float(tech['sma_20']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_20'])) / safe_float(tech['sma_20']) * 100:+.2f}%
    - 50周期: {safe_float(tech['sma_50']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_50'])) / safe_float(tech['sma_50']) * 100:+.2f}%

    🎯 趋势分析:
    - 短期趋势: {trend.get('short_term', 'N/A')}
    - 中期趋势: {trend.get('medium_term', 'N/A')}
    - 整体趋势: {trend.get('overall', 'N/A')}
    - MACD方向: {trend.get('macd', 'N/A')}

    📊 动量指标:
    - RSI: {safe_float(tech['rsi']):.2f} ({'超买' if safe_float(tech['rsi']) > 70 else '超卖' if safe_float(tech['rsi']) < 30 else '中性'})
    - MACD: {safe_float(tech['macd']):.4f}
    - 信号线: {safe_float(tech['macd_signal']):.4f}

    🎚️ 布林带位置: {safe_float(tech['bb_position']):.2%} ({'上部' if safe_float(tech['bb_position']) > 0.7 else '下部' if safe_float(tech['bb_position']) < 0.3 else '中部'})

    💰 关键水平:
    - 静态阻力: {safe_float(levels.get('static_resistance', 0)):.2f}
    - 静态支撑: {safe_float(levels.get('static_support', 0)):.2f}
    """
    return analysis_text


def get_current_position():
    """获取当前持仓情况 - OKX版本"""
    try:
        positions = exchange.fetch_positions([TRADE_CONFIG['symbol']])

        for pos in positions:
            if pos['symbol'] == TRADE_CONFIG['symbol']:
                contracts = float(pos['contracts']) if pos['contracts'] else 0

                if contracts > 0:
                    return {
                        'side': pos['side'],  # 'long' or 'short'
                        'size': contracts,
                        'entry_price': float(pos['entryPrice']) if pos['entryPrice'] else 0,
                        'unrealized_pnl': float(pos['unrealizedPnl']) if pos['unrealizedPnl'] else 0,
                        'leverage': float(pos['leverage']) if pos['leverage'] else 5,  # 默认5倍杠杆
                        'symbol': pos['symbol']
                    }

        return None

    except Exception as e:
        print(f"获取持仓失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def safe_json_parse(json_str):
    """安全解析JSON，处理格式不规范的情况"""
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        try:
            # 修复常见的JSON格式问题
            json_str = json_str.replace("'", '"')
            json_str = re.sub(r'(\w+):', r'"\1":', json_str)
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"JSON解析失败，原始内容: {json_str}")
            print(f"错误详情: {e}")
            return None


def create_fallback_signal(price_data):
    """创建备用交易信号"""
    # 🔴 修复：处理 price_data 为空的情况
    if not price_data or not isinstance(price_data, dict):
        price_data = {'price': 0}
    
    return {
        "signal": "HOLD",
        "reason": "因技术分析暂时不可用，采取保守策略",
        "stop_loss": price_data['price'] * 0.98,  # -2%
        "take_profit": price_data['price'] * 1.02,  # +2%
        "confidence": "LOW",
        "is_fallback": True
    }


def safe_get_value(data, key, default=None):
    """安全获取字典值，防止NoneType错误"""
    try:
        if data is None:
            return default
        if isinstance(data, dict):
            return data.get(key, default)
        return default
    except Exception as e:
        print(f"安全获取值失败: {e}")
        return default


def validate_stop_loss_take_profit(signal_data, price_data, side):
    """验证止盈止损价格的合理性"""
    current_price = price_data['price']
    stop_loss = signal_data.get('stop_loss', 0)
    take_profit = signal_data.get('take_profit', 0)
    
    print(f"🔍 验证止盈止损价格:")
    print(f"   - 当前价格: {current_price:.2f}")
    print(f"   - 止损价格: {stop_loss:.2f}")
    print(f"   - 止盈价格: {take_profit:.2f}")
    print(f"   - 交易方向: {side}")
    
    # 基本验证
    if stop_loss <= 0 or take_profit <= 0:
        print("❌ 止盈止损价格无效")
        return False, None, None
    
    # 多空方向验证
    if side == 'long':
        if stop_loss >= current_price:
            print("❌ 多头止损价格不能高于当前价格")
            return False, None, None
        if take_profit <= current_price:
            print("❌ 多头止盈价格不能低于当前价格")
            return False, None, None
    elif side == 'short':
        if stop_loss <= current_price:
            print("❌ 空头止损价格不能低于当前价格")
            return False, None, None
        if take_profit >= current_price:
            print("❌ 空头止盈价格不能高于当前价格")
            return False, None, None
    
    # 风险收益比验证
    if side == 'long':
        risk = current_price - stop_loss
        reward = take_profit - current_price
    else:
        risk = stop_loss - current_price
        reward = current_price - take_profit
    
    risk_reward_ratio = reward / risk if risk > 0 else 0
    print(f"   - 风险: {risk:.2f}")
    print(f"   - 收益: {reward:.2f}")
    print(f"   - 风险收益比: {risk_reward_ratio:.2f}")
    
    if risk_reward_ratio < 1.0:  # 至少1:1的风险收益比
        print(f"⚠️ 风险收益比过低: {risk_reward_ratio:.2f}")
        # 可以选择继续或拒绝
    
    print("✅ 止盈止损价格验证通过")
    return True, stop_loss, take_profit


def calculate_dynamic_risk_reward_ratio(price_data):
    """计算动态风险收益比"""
    config = TRADE_CONFIG.get('risk_reward', {})
    
    if not config.get('enable_dynamic_rr', True):
        return 3  # 默认1:3
    
    trend = price_data.get('trend_analysis', {}).get('overall', '')
    
    if trend == '强势上涨':
        return 5  # 1:5
    elif trend == '强势下跌':
        return 5  # 1:5
    elif trend == '震荡整理':
        return 1.5  # 1:1.5
    else:
        return config.get('default', 3)  # 默认1:3


def calculate_dynamic_stop_loss_take_profit(signal_data, price_data, side, leverage):
    """动态计算止盈止损点位（支持动态风险收益比）"""
    current_price = price_data['price']
    confidence = signal_data.get('confidence', 'MEDIUM')
    
    # 使用ATR计算更合理的止损（避免过紧止损）
    atr_ratio = 0.015  # 默认止损比例
    if 'technical_data' in price_data:
        atr = price_data['technical_data'].get('atr_20', 0)
        current_price = price_data['price']
        if atr > 0 and current_price > 0:
            # 使用2-3倍ATR作为止损范围（给趋势空间）
            atr_ratio = (atr * 2.5) / current_price
            # 限制在合理范围
            atr_ratio = max(0.005, min(0.03, atr_ratio))
    
    # 基础止损比例（基于ATR或杠杆）
    base_stop_loss_ratio = max(atr_ratio, 0.02 / leverage)
    
    # 根据信心程度调整止损比例
    confidence_multiplier = {
        'HIGH': 0.8,    # 高信心时止损更紧
        'MEDIUM': 1.0,   # 中等信心
        'LOW': 1.2       # 低信心时止损更宽
    }.get(confidence, 1.0)
    
    # 根据市场波动调整
    volatility_multiplier = 1.0
    if 'technical_data' in price_data:
        bb_upper = price_data['technical_data'].get('bb_upper', 0)
        bb_lower = price_data['technical_data'].get('bb_lower', 0)
        if bb_upper > 0 and bb_lower > 0:
            bb_width = (bb_upper - bb_lower) / current_price
            if bb_width > 0.05:  # 高波动
                volatility_multiplier = 1.3
            elif bb_width < 0.02:  # 低波动
                volatility_multiplier = 0.8
    
    # 计算最终止损比例
    final_stop_loss_ratio = base_stop_loss_ratio * confidence_multiplier * volatility_multiplier
    
    # 🆕 使用动态风险收益比（而不是固定1:2）
    dynamic_rr = calculate_dynamic_risk_reward_ratio(price_data)
    take_profit_ratio = final_stop_loss_ratio * dynamic_rr
    
    # 计算具体价格
    if side == 'long':
        stop_loss_price = current_price * (1 - final_stop_loss_ratio)
        take_profit_price = current_price * (1 + take_profit_ratio)
    else:  # short
        stop_loss_price = current_price * (1 + final_stop_loss_ratio)
        take_profit_price = current_price * (1 - take_profit_ratio)
    
    print(f"📊 动态止盈止损计算（利益最大化版）:")
    print(f"   - 基础止损比例: {base_stop_loss_ratio:.3f}")
    print(f"   - 信心倍数: {confidence_multiplier}")
    print(f"   - 波动倍数: {volatility_multiplier}")
    print(f"   - 最终止损比例: {final_stop_loss_ratio:.3f}")
    print(f"   - 动态风险收益比: 1:{dynamic_rr}")
    print(f"   - 止盈比例: {take_profit_ratio:.3f}")
    print(f"   - 止损价格: {stop_loss_price:.2f}")
    print(f"   - 止盈价格: {take_profit_price:.2f}")
    
    return stop_loss_price, take_profit_price


def update_trailing_stop(current_position, price_data):
    """移动止损机制 - 锁定利润并让利润奔跑"""
    config = TRADE_CONFIG.get('trailing_stop', {})
    
    if not config.get('enable_trailing_stop', True):
        return None
    
    if not current_position:
        return None
    
    entry_price = current_position.get('entry_price', 0)
    side = current_position.get('side', '')
    current_price = price_data['price']
    
    if entry_price <= 0:
        return None
    
    # 计算浮盈百分比
    if side == 'long':
        unrealized_pnl_pct = (current_price - entry_price) / entry_price
    else:  # short
        unrealized_pnl_pct = (entry_price - current_price) / entry_price
    
    # 获取当前止损
    current_sl = position_management.get('current_stop_loss')
    initial_sl = position_management.get('initial_stop_loss', current_sl)
    
    # 计算新的止损价格
    new_stop_loss = None
    
    # 浮盈20%时，锁定10%利润
    if unrealized_pnl_pct >= config.get('lock_profit_2_threshold', 0.20):
        lock_level = config.get('lock_profit_2_level', 0.10)
        if side == 'long':
            new_stop_loss = entry_price * (1 + lock_level)
        else:
            new_stop_loss = entry_price * (1 - lock_level)
        print(f"📈 浮盈{unrealized_pnl_pct*100:.1f}%，移动止损到锁定{lock_level*100:.1f}%利润: {new_stop_loss:.2f}")
    
    # 浮盈10%时，锁定3%利润
    elif unrealized_pnl_pct >= config.get('lock_profit_1_threshold', 0.10):
        lock_level = config.get('lock_profit_1_level', 0.03)
        if side == 'long':
            new_stop_loss = entry_price * (1 + lock_level)
        else:
            new_stop_loss = entry_price * (1 - lock_level)
        print(f"📈 浮盈{unrealized_pnl_pct*100:.1f}%，移动止损到锁定{lock_level*100:.1f}%利润: {new_stop_loss:.2f}")
    
    # 浮盈5%时，止损移到成本价（保本）
    elif unrealized_pnl_pct >= config.get('breakeven_threshold', 0.05):
        new_stop_loss = entry_price
        print(f"📈 浮盈{unrealized_pnl_pct*100:.1f}%，移动止损到成本价保本: {new_stop_loss:.2f}")
    
    # 如果计算出了新止损，且比当前止损更优，则更新
    if new_stop_loss:
        # 确保新止损不会劣于当前止损
        if side == 'long':
            if current_sl is None or new_stop_loss > current_sl:
                position_management['current_stop_loss'] = new_stop_loss
                return new_stop_loss
        else:  # short
            if current_sl is None or new_stop_loss < current_sl:
                position_management['current_stop_loss'] = new_stop_loss
                return new_stop_loss
    
    return None


def check_pyramid_add(current_position, price_data, signal_data):
    """金字塔加仓检查 - 趋势中扩大收益"""
    config = TRADE_CONFIG['position_management']
    
    if not config.get('enable_pyramid', True):
        return False
    
    if not current_position:
        return False
    
    # 检查是否达到最大加仓次数
    if position_management['pyramid_count'] >= config.get('max_pyramid_times', 2):
        return False
    
    entry_price = current_position.get('entry_price', 0)
    side = current_position.get('side', '')
    current_price = price_data['price']
    
    if entry_price <= 0:
        return False
    
    # 计算浮盈百分比
    if side == 'long':
        unrealized_pnl_pct = (current_price - entry_price) / entry_price
    else:
        unrealized_pnl_pct = (entry_price - current_price) / entry_price
    
    # 检查是否达到加仓阈值
    pyramid_threshold = config.get('pyramid_threshold', 0.05)
    if unrealized_pnl_pct < pyramid_threshold:
        return False
    
    # 检查信号方向是否一致
    desired_signal = signal_data.get('signal', '')
    if side == 'long' and desired_signal != 'BUY':
        return False
    if side == 'short' and desired_signal != 'SELL':
        return False
    
    # 检查趋势是否延续
    trend = price_data.get('trend_analysis', {}).get('overall', '')
    if side == 'long' and trend not in ['强势上涨']:
        return False
    if side == 'short' and trend not in ['强势下跌']:
        return False
    
    # 检查信心程度（加仓需要中等以上信心）
    confidence = signal_data.get('confidence', 'LOW')
    if confidence == 'LOW':
        return False
    
    print(f"✅ 满足加仓条件: 浮盈{unrealized_pnl_pct*100:.1f}%, 趋势延续, 信号一致")
    return True


def execute_partial_take_profit(current_position, price_data, initial_stop_loss):
    """分批止盈执行 - 优化收益曲线"""
    config = TRADE_CONFIG.get('partial_take_profit', {})
    
    if not config.get('enable_partial_tp', True):
        return
    
    if not current_position:
        return
    
    entry_price = current_position.get('entry_price', 0)
    side = current_position.get('side', '')
    current_price = price_data['price']
    position_size = current_position.get('size', 0)
    
    if entry_price <= 0 or position_size <= 0:
        return
    
    # 计算初始风险（用于计算风险收益比）
    if side == 'long':
        initial_risk = entry_price - initial_stop_loss
        current_profit_pct = (current_price - entry_price) / entry_price
    else:
        initial_risk = initial_stop_loss - entry_price
        current_profit_pct = (entry_price - current_price) / entry_price
    
    if initial_risk <= 0:
        return
    
    # 检查TP1：30%仓位在1.5倍风险收益比止盈
    if not position_management['partial_tp_executed']['tp1']:
        tp1_rr = config.get('tp1_rr_multiplier', 1.5)
        tp1_target = initial_risk * tp1_rr
        
        if side == 'long' and (current_price - entry_price) >= tp1_target:
            tp1_amount = position_size * config.get('tp1_ratio', 0.3)
            try:
                print(f"💰 执行第一批止盈(30%): 价格{current_price:.2f}, 数量{tp1_amount:.2f}")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'], 'sell' if side == 'long' else 'buy',
                    tp1_amount, None, {
                        'reduceOnly': True,
                        'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                        'posSide': side
                    }
                )
                position_management['partial_tp_executed']['tp1'] = True
            except Exception as e:
                print(f"⚠️ 分批止盈TP1执行失败: {e}")
        
        elif side == 'short' and (entry_price - current_price) >= tp1_target:
            tp1_amount = position_size * config.get('tp1_ratio', 0.3)
            try:
                print(f"💰 执行第一批止盈(30%): 价格{current_price:.2f}, 数量{tp1_amount:.2f}")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'], 'buy',
                    tp1_amount, None, {
                        'reduceOnly': True,
                        'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                        'posSide': side
                    }
                )
                position_management['partial_tp_executed']['tp1'] = True
            except Exception as e:
                print(f"⚠️ 分批止盈TP1执行失败: {e}")
    
    # 检查TP2：30%仓位在2.5倍风险收益比止盈
    if position_management['partial_tp_executed']['tp1'] and not position_management['partial_tp_executed']['tp2']:
        tp2_rr = config.get('tp2_rr_multiplier', 2.5)
        tp2_target = initial_risk * tp2_rr
        remaining_size = position_size * (1 - config.get('tp1_ratio', 0.3))
        
        if side == 'long' and (current_price - entry_price) >= tp2_target:
            tp2_amount = remaining_size * (config.get('tp2_ratio', 0.3) / (1 - config.get('tp1_ratio', 0.3)))
            try:
                print(f"💰 执行第二批止盈(30%): 价格{current_price:.2f}, 数量{tp2_amount:.2f}")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'], 'sell' if side == 'long' else 'buy',
                    tp2_amount, None, {
                        'reduceOnly': True,
                        'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                        'posSide': side
                    }
                )
                position_management['partial_tp_executed']['tp2'] = True
            except Exception as e:
                print(f"⚠️ 分批止盈TP2执行失败: {e}")
        
        elif side == 'short' and (entry_price - current_price) >= tp2_target:
            tp2_amount = remaining_size * (config.get('tp2_ratio', 0.3) / (1 - config.get('tp1_ratio', 0.3)))
            try:
                print(f"💰 执行第二批止盈(30%): 价格{current_price:.2f}, 数量{tp2_amount:.2f}")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'], 'buy',
                    tp2_amount, None, {
                        'reduceOnly': True,
                        'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                        'posSide': side
                    }
                )
                position_management['partial_tp_executed']['tp2'] = True
            except Exception as e:
                print(f"⚠️ 分批止盈TP2执行失败: {e}")


def analyze_with_deepseek(price_data):
    """使用DeepSeek分析市场并生成交易信号（增强版）"""

    # 🔴 修复：添加空值检查
    if not price_data or not isinstance(price_data, dict):
        print("❌ price_data 为空或无效，使用备用信号")
        return create_fallback_signal({'price': 0})

    print("🤖 开始调用DeepSeek API分析市场...")
    
    # 生成技术分析文本
    technical_analysis = generate_technical_analysis_text(price_data)

    # 构建K线数据文本
    kline_text = f"【最近5根{TRADE_CONFIG['timeframe']}K线数据】\n"
    
    # 🔴 修复：检查 kline_data 是否存在且不为空
    if 'kline_data' in price_data and price_data['kline_data'] is not None:
        kline_data = price_data['kline_data']
        if isinstance(kline_data, list) and len(kline_data) > 0:
            for i, kline in enumerate(kline_data[-5:]):
                if isinstance(kline, dict) and 'close' in kline and 'open' in kline:
                    trend = "阳线" if kline['close'] > kline['open'] else "阴线"
                    change = ((kline['close'] - kline['open']) / kline['open']) * 100
                    kline_text += f"K线{i + 1}: {trend} 开盘:{kline['open']:.2f} 收盘:{kline['close']:.2f} 涨跌:{change:+.2f}%\n"
                else:
                    kline_text += f"K线{i + 1}: 数据格式错误\n"
        else:
            kline_text += "K线数据为空\n"
    else:
        kline_text += "K线数据不可用\n"

    # 添加上次交易信号
    signal_text = ""
    if signal_history and len(signal_history) > 0:
        last_signal = signal_history[-1]
        if isinstance(last_signal, dict):
            signal_text = f"\n【上次交易信号】\n信号: {last_signal.get('signal', 'N/A')}\n信心: {last_signal.get('confidence', 'N/A')}"
        else:
            signal_text = "\n【上次交易信号】\n数据格式错误"

    # 添加当前持仓信息
    current_pos = get_current_position()
    position_text = "无持仓" if not current_pos else f"{current_pos['side']}仓, 数量: {current_pos['size']}, 盈亏: {current_pos['unrealized_pnl']:.2f}USDT"

    prompt = f"""
    你是一位拥有15年经验的顶级加密货币量化交易员，你拥有INTJ 人格特征，是天生的系统构建者和长期规划者。并专精于BTC/USDT合约交易,善于洞察市场潜在机会，更懂得提前预知黑天鹅事件，并有效控制风险，目的是让资产最大化。

    【数据概览】
    基于以下BTC/USDT {TRADE_CONFIG['timeframe']}周期数据进行分析：

    {kline_text}

    {technical_analysis}

    {signal_text}

    【当前行情】
    - 当前价格: ${price_data['price']:,.2f}
    - 时间: {price_data['timestamp']}
    - 本K线最高: ${price_data['high']:,.2f}
    - 本K线最低: ${price_data['low']:,.2f}
    - 本K线成交量: {price_data['volume']:.2f} BTC
    - 价格变化: {price_data['price_change']:+.2f}%
    - 当前持仓: {position_text}
    - 持仓盈亏: {(current_pos['unrealized_pnl'] if current_pos else 0):.2f} USDT

    【思维链分析要求 - 请按以下步骤逐步分析】

    **第一步：多空力量对比分析**
    1. 分析最近5根K线的多空力量变化
    2. 评估成交量与价格的关系（量价配合度）
    3. 判断当前是多头主导还是空头主导
    4. 识别是否有力量转换的迹象

    **第二步：关键指标状态评估**
    1. 均线系统：分析价格与各均线的关系，判断趋势强度
    2. RSI指标：评估超买超卖状态和动量变化
    3. MACD指标：分析趋势方向和动能强弱
    4. 布林带：判断价格位置和波动性
    5. 支撑阻力：识别关键价位和突破情况

    **第三步：市场结构分析**
    1. 趋势结构：判断当前处于趋势的哪个阶段
    2. 波动特征：分析市场波动率和风险水平
    3. 时间周期：考虑不同时间框架的共振情况
    4. 市场情绪：基于技术指标推断市场情绪状态

    **第四步：风险收益评估**
    1. 当前信号的风险收益比
    2. 止损止盈位置的合理性
    3. 市场环境是否适合交易
    4. 与历史信号的对比分析

    **第五步：综合决策**
    基于以上四步分析，给出最终的交易决策

    【防频繁交易重要原则】
    1. **趋势持续性优先**: 不要因单根K线或短期波动改变整体趋势判断
    2. **持仓稳定性**: 除非趋势明确强烈反转，否则保持现有持仓方向
    3. **反转确认**: 需要至少2-3个技术指标同时确认趋势反转才改变信号
    4. **成本意识**: 减少不必要的仓位调整，每次交易都有成本

    【交易指导原则 - 必须遵守】
    1. **趋势优先法则（最重要）**: 
       - 当短期+中期趋势同向时（强势上涨/下跌），必须给出明确的BUY/SELL信号
       - 不要因为RSI、MACD等指标有轻微偏差就选择HOLD
       - HOLD仅用于：趋势完全矛盾（短期上涨+中期下跌且幅度相近）或价格在窄幅区间震荡（波动<2%）
    
    2. **积极判断原则**:
       - 强势上涨趋势 + RSI在30-75区间 → BUY（HIGH/MEDIUM信心）
       - 强势上涨趋势 + RSI>75 → BUY（MEDIUM信心，注意回调风险，但仍应给出信号）
       - 强势下跌趋势 + RSI在25-70区间 → SELL（HIGH/MEDIUM信心）
       - 强势下跌趋势 + RSI<25 → SELL（MEDIUM信心，注意反弹风险，但仍应给出信号）
       - 仅当价格在20周期高低点之间窄幅震荡（幅度<2%）且多空力量平衡 → HOLD
    
    3. **BTC特性**: 因为做的是BTC，做多权重可以适当增加，在上涨趋势中更积极
    
    4. **技术指标权重和解读**:
       - 趋势(均线排列) > 支撑阻力突破 > RSI > MACD > 布林带
       - 指标用于验证趋势，而不是否定明确的趋势
       - 价格突破关键支撑/阻力位是强信号（不管RSI如何，都应给出BUY/SELL）
    
    5. **信心等级标准**:
       - HIGH: 趋势明确 + 多个指标共振 + 量价配合
       - MEDIUM: 趋势明确 + 部分指标支持（即使某些指标有轻微偏差）
       - LOW: 趋势不明确或指标完全矛盾

    【当前技术状况快速参考】
    - 整体趋势: {price_data.get('trend_analysis', {}).get('overall', 'N/A') if price_data.get('trend_analysis') else 'N/A'}
    - 短期趋势: {price_data.get('trend_analysis', {}).get('short_term', 'N/A') if price_data.get('trend_analysis') else 'N/A'} 
    - RSI状态: {(price_data.get('technical_data', {}).get('rsi', 0) if price_data.get('technical_data') else 0):.1f} ({'超买' if (price_data.get('technical_data', {}).get('rsi', 0) if price_data.get('technical_data') else 0) > 70 else '超卖' if (price_data.get('technical_data', {}).get('rsi', 0) if price_data.get('technical_data') else 0) < 30 else '中性'})
    - MACD方向: {price_data.get('trend_analysis', {}).get('macd', 'N/A') if price_data.get('trend_analysis') else 'N/A'}

    【输出格式要求】
    请严格按照以下JSON格式回复，reason字段必须包含完整的思维链分析过程：

    {{
        "signal": "BUY|SELL|HOLD",
        "reason": "【思维链分析】第一步：多空力量对比...第二步：关键指标状态...第三步：市场结构分析...第四步：风险收益评估...第五步：综合决策...",
        "stop_loss": 具体价格,
        "take_profit": 具体价格, 
        "confidence": "HIGH|MEDIUM|LOW"
    }}
    """

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system",
                 "content": f"""您是一位拥有15年经验的顶级加密货币量化交易员，拥有INTJ人格特征，是天生的系统构建者和长期规划者。专注于{TRADE_CONFIG['timeframe']}周期趋势分析。

【核心能力】
- 深度技术分析：能够从多个维度分析市场
- 结构化思维：按照思维链逐步分析问题
- 风险控制：始终将风险控制放在首位，但不过度保守而错失机会
- 逻辑推理：基于数据做出理性决策
- 机会捕捉：在风险可控的前提下，积极捕捉明确的趋势机会

【分析要求】
请严格按照思维链分析要求，逐步完成五个步骤的分析：
1. 多空力量对比分析
2. 关键指标状态评估  
3. 市场结构分析
4. 风险收益评估
5. 综合决策

【决策原则】
- **趋势是王道**：明确的趋势信号优先于指标的细微偏差
- **概率思维**：追求概率优势，而非绝对确定性
- **风险可控**：每笔交易都有止损保护，不要因过度谨慎而错过明确的趋势机会

【输出标准】
- reason字段必须包含完整的五步思维链分析
- 每个步骤都要有具体的分析内容
- 最终决策要有明确的逻辑依据
- 严格遵循JSON格式要求
- **重要**：当趋势明确时，即使个别指标有轻微偏差，也要给出明确的BUY/SELL信号，而不是HOLD"""},
                {"role": "user", "content": prompt}
            ],
            stream=False,
            temperature=0.3  # 从0.1提高到0.3，平衡保守和灵活性，让AI在趋势明确时更积极
        )

        # 安全解析JSON
        if not response or not hasattr(response, 'choices') or not response.choices or len(response.choices) == 0:
            print("❌ DeepSeek API 响应为空或格式错误")
            return create_fallback_signal(price_data)
            
        if not hasattr(response.choices[0], 'message') or not response.choices[0].message:
            print("❌ DeepSeek API 响应消息为空")
            return create_fallback_signal(price_data)
            
        result = response.choices[0].message.content
        if not result:
            print("❌ DeepSeek API 响应内容为空")
            return create_fallback_signal(price_data)
            
        print(f"DeepSeek原始回复: {result}")

        # 提取JSON部分
        start_idx = result.find('{')
        end_idx = result.rfind('}') + 1

        if start_idx != -1 and end_idx != 0:
            json_str = result[start_idx:end_idx]
            signal_data = safe_json_parse(json_str)

            if signal_data is None:
                signal_data = create_fallback_signal(price_data)
        else:
            signal_data = create_fallback_signal(price_data)

        # 验证必需字段
        required_fields = ['signal', 'reason', 'stop_loss', 'take_profit', 'confidence']
        if not all(field in signal_data for field in required_fields):
            signal_data = create_fallback_signal(price_data)

        # 保存信号到历史记录
        signal_data['timestamp'] = price_data['timestamp']
        signal_history.append(signal_data)
        if len(signal_history) > 30:
            signal_history.pop(0)

        # 信号统计
        signal_count = len([s for s in signal_history if s.get('signal') == signal_data['signal']])
        total_signals = len(signal_history)
        print(f"信号统计: {signal_data['signal']} (最近{total_signals}次中出现{signal_count}次)")

        # 信号连续性检查
        if len(signal_history) >= 3:
            last_three = []
            for s in signal_history[-3:]:
                if isinstance(s, dict) and 'signal' in s:
                    last_three.append(s['signal'])
            if len(last_three) == 3 and len(set(last_three)) == 1:
                print(f"⚠️ 注意：连续3次{signal_data['signal']}信号")

        return signal_data

    except Exception as e:
        print(f"DeepSeek分析失败: {e}")
        import traceback
        print(f"详细错误信息: {traceback.format_exc()}")
        return create_fallback_signal(price_data)


def execute_trade(signal_data, price_data):
    """执行交易 - OKX版本（集成原子化止盈止损）"""
    global position

    current_position = get_current_position()

    # ========== 交易频次控制与同向处理（节流） ==========
    # 波动分档选择参数
    tech = price_data.get('technical_data', {})
    bb_upper = tech.get('bb_upper', 0)
    bb_lower = tech.get('bb_lower', 0)
    atr_ratio = tech.get('atr_ratio', 0)
    current_price = price_data['price']

    bb_width_ratio = 0
    if current_price > 0 and bb_upper and bb_lower:
        bb_width_ratio = (bb_upper - bb_lower) / current_price

    # 判定分档（优先ATR，其次BB宽度）
    if atr_ratio and atr_ratio > 0:
        if atr_ratio < TRADE_THROTTLE['low_atr_ratio']:
            regime = 'low'
        elif atr_ratio > TRADE_THROTTLE['high_atr_ratio']:
            regime = 'high'
        else:
            regime = 'mid'
    else:
        if bb_width_ratio < TRADE_THROTTLE['low_bb_width']:
            regime = 'low'
        elif bb_width_ratio > TRADE_THROTTLE['high_bb_width']:
            regime = 'high'
        else:
            regime = 'mid'

    persist_need = TRADE_THROTTLE[regime]['persist']
    cooldown_need = TRADE_THROTTLE[regime]['cooldown']
    min_move_atr = TRADE_THROTTLE[regime]['min_move_atr']
    max_trades_day = TRADE_THROTTLE[regime]['max_trades_day']

    def _same_signal_persisted(required, desired):
        if len(signal_history) < required:
            return False
        last = [s.get('signal') for s in signal_history[-required:]]
        return all(sig == desired for sig in last)

    def _in_cooldown(curr_bar, cooldown):
        li = last_trade_info.get('bar_index')
        if li is None:
            return False
        return (curr_bar - li) < cooldown

    def _daily_quota_ok():
        today = datetime.now().strftime('%Y-%m-%d')
        if last_trade_info.get('date') != today:
            last_trade_info['date'] = today
            last_trade_info['count_today'] = 0
        return last_trade_info['count_today'] < max_trades_day

    def _min_move_ok(curr_price):
        lp = last_trade_info.get('price')
        atr = tech.get('atr_20', 0)
        if not lp or not atr or atr <= 0:
            return True
        return abs(curr_price - lp) >= (min_move_atr * atr)

    # bar索引（按15m整点）
    curr_bar_index = int(datetime.now().timestamp() // (15 * 60))

    desired_signal = signal_data['signal']
    want_side = 'long' if desired_signal == 'BUY' else ('short' if desired_signal == 'SELL' else None)

    # 🆕 同向持仓时的处理：检查是否满足加仓条件
    if current_position and want_side and current_position['side'] == want_side:
        # 检查是否可以加仓（金字塔加仓）
        if check_pyramid_add(current_position, price_data, signal_data):
            print("🎯 满足加仓条件，执行金字塔加仓...")
            # 计算加仓金额
            config = TRADE_CONFIG['position_management']
            base_usdt = config['base_usdt_amount']
            pyramid_ratio = config.get('pyramid_amount_ratio', 0.3)
            add_amount_usdt = base_usdt * pyramid_ratio  # 加仓金额为原仓位的30%
            
            # 计算加仓合约数量
            current_price = price_data['price']
            contract_size = TRADE_CONFIG.get('contract_size', 0.001)
            dynamic_leverage = calculate_dynamic_leverage(signal_data, price_data)
            add_contracts = (add_amount_usdt * dynamic_leverage) / (current_price * contract_size)
            add_contracts = round(add_contracts, 2)
            
            try:
                if want_side == 'long':
                    order = exchange.create_market_order(
                        TRADE_CONFIG['symbol'], 'buy', add_contracts, None, {
                            'posSide': 'long',
                            'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                            'ordType': 'market'
                        }
                    )
                else:
                    order = exchange.create_market_order(
                        TRADE_CONFIG['symbol'], 'sell', add_contracts, None, {
                            'posSide': 'short',
                            'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                            'ordType': 'market'
                        }
                    )
                position_management['pyramid_count'] += 1
                print(f"✅ 加仓成功: {add_contracts:.2f} 张 (第{position_management['pyramid_count']}次加仓)")
            except Exception as e:
                print(f"❌ 加仓失败: {e}")
        
        # 🆕 检查并更新移动止损
        new_sl = update_trailing_stop(current_position, price_data)
        if new_sl:
            # 更新交易所止损订单
            try:
                # 取消旧止损
                cleanup_stop_loss_orders()
                # 设置新止损（需要根据实际持仓数量）
                print(f"🔄 更新止损到: {new_sl:.2f}")
            except Exception as e:
                print(f"⚠️ 更新止损失败: {e}")
        
        # 🆕 检查并执行分批止盈
        initial_sl = position_management.get('initial_stop_loss')
        if initial_sl:
            execute_partial_take_profit(current_position, price_data, initial_sl)
        
        print("已有同向持仓，完成加仓/止损/止盈检查")
        return

    # 信号持久性
    if want_side and not _same_signal_persisted(persist_need, desired_signal):
        print("信号未达到持久性要求，跳过开仓")
        return

    # 冷却与日上限
    if _in_cooldown(curr_bar_index, cooldown_need):
        print("处于交易冷却期，跳过开仓")
        return
    if not _daily_quota_ok():
        print("达到当日交易上限，跳过开仓")
        return

    # 最小变动阈值（基于ATR）
    if not _min_move_ok(current_price):
        print("价格变动不足（ATR阈值），跳过开仓")
        return

    # 🔧 优化：放宽反转限制 - 趋势明确时允许MEDIUM信心执行
    if current_position and signal_data['signal'] != 'HOLD':
        current_side = current_position['side']
        # 修正：正确处理HOLD情况
        if signal_data['signal'] == 'BUY':
            new_side = 'long'
        elif signal_data['signal'] == 'SELL':
            new_side = 'short'
        else:  # HOLD
            new_side = None

        # 如果只是方向反转，检查趋势和信心
        if new_side != current_side:
            # 检查趋势是否明确
            trend_overall = price_data.get('trend_analysis', {}).get('overall', '')
            is_clear_trend = trend_overall in ['强势上涨', '强势下跌']
            
            # 趋势明确时，MEDIUM信心也可以反转
            # 趋势不明确时，需要HIGH信心才反转
            if signal_data['confidence'] == 'LOW':
                print(f"🔒 低信心反转信号，保持现有{current_side}仓")
                return
            elif signal_data['confidence'] == 'MEDIUM' and not is_clear_trend:
                print(f"🔒 中信心但趋势不明确，保持现有{current_side}仓")
                return
            
            # 检查最近信号历史，避免频繁反转（但允许趋势延续时的重复信号）
            if len(signal_history) >= 3:
                last_three_signals = [s['signal'] for s in signal_history[-3:]]
                # 如果最近3次都是同一个方向的信号，说明趋势延续，允许执行
                if len(set(last_three_signals)) == 1 and last_three_signals[0] != signal_data['signal']:
                    # 相反方向的信号已经出现3次，可能是趋势反转，允许执行
                    pass
                elif last_three_signals[-1] == signal_data['signal'] and last_three_signals[-2] == signal_data['signal']:
                    # 相同信号连续出现，如果是趋势延续，应该执行，不拦截
                    pass
                elif signal_data['confidence'] != 'HIGH':
                    # 频繁切换且非高信心，保持谨慎
                    print(f"🔒 信号频繁切换且非高信心，保持现有持仓")
                    return

    print(f"交易信号: {signal_data['signal']}")
    print(f"信心程度: {signal_data['confidence']}")
    print(f"理由: {signal_data['reason']}")
    print(f"AI建议止损: ${signal_data['stop_loss']:,.2f}")
    print(f"AI建议止盈: ${signal_data['take_profit']:,.2f}")
    print(f"当前持仓: {current_position}")

    # 风险管理：低信心信号的处理策略（优化版）
    if signal_data['confidence'] == 'LOW':
        # 检查是否有明确趋势支持
        trend_overall = price_data.get('trend_analysis', {}).get('overall', '')
        is_clear_trend = trend_overall in ['强势上涨', '强势下跌']
        
        if is_clear_trend and signal_data['signal'] != 'HOLD':
            # 趋势明确但信心低，可能是指标有偏差，允许小仓位尝试
            print("⚠️ 低信心但趋势明确，将通过仓位管理降低风险（使用最低仓位）")
            # 不return，继续执行，但在calculate_intelligent_position中会自动使用最低仓位
        elif not TRADE_CONFIG['test_mode']:
            # 趋势不明确且低信心，跳过
            print("⚠️ 低信心且趋势不明确，跳过执行")
            return

    if TRADE_CONFIG['test_mode']:
        print("测试模式 - 仅模拟交易")
        return

    try:
        # 🔧 修复：先判断信号，再执行相关逻辑
        if signal_data['signal'] == 'BUY':
            side = 'long'
            
            # 🆕 使用智能仓位计算（包含动态杠杆）
            order_amount, dynamic_leverage = calculate_intelligent_position(signal_data, price_data, current_position)
            
            # 🆕 动态设置杠杆（变化显著时才设置，避免触发不必要影响）
            curr_lev = (current_position or {}).get('leverage')
            if curr_lev is None or abs(dynamic_leverage - float(curr_lev)) >= TRADE_THROTTLE['leverage_tol']:
                print(f"🔧 设置动态杠杆: {dynamic_leverage}倍")
                leverage_success = safe_set_leverage(
                    dynamic_leverage,
                    TRADE_CONFIG['symbol'],
                    TRADE_CONFIG.get('td_mode', 'cross')
                )
            else:
                print("杠杆变化不显著，跳过设置杠杆")
                leverage_success = True
            
            if not leverage_success:
                print("⚠️ 杠杆设置失败，使用默认杠杆")
                dynamic_leverage = 5  # 使用默认杠杆
            
            # 获取账户余额进行最终检查
            balance = exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            required_margin = price_data['price'] * order_amount * TRADE_CONFIG['contract_size'] / dynamic_leverage
            
            if required_margin > usdt_balance * 0.8:  # 使用不超过80%的余额
                print(f"⚠️ 保证金不足，跳过交易。需要: {required_margin:.2f} USDT, 可用: {usdt_balance:.2f} USDT")
                return
            
            # 🆕 动态计算止盈止损
            stop_loss_price, take_profit_price = calculate_dynamic_stop_loss_take_profit(
                signal_data, price_data, side, dynamic_leverage
            )
            
            # 🆕 验证止盈止损价格
            is_valid, validated_sl, validated_tp = validate_stop_loss_take_profit(
                {'stop_loss': stop_loss_price, 'take_profit': take_profit_price}, 
                price_data, side
            )
            
            if not is_valid:
                print("❌ 止盈止损验证失败，取消交易")
                return
            
            if current_position and current_position['side'] == 'short':
                print("平空仓...")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'], 'buy', current_position['size'], 
                    None, {
                        'reduceOnly': True,
                        'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                        'posSide': 'short'
                    }
                )
                time.sleep(2)  # 等待平仓完成
                # 🆕 重置持仓管理状态
                position_management['pyramid_count'] = 0
                position_management['partial_tp_executed'] = {'tp1': False, 'tp2': False, 'tp3': False}

            print("开多仓并设置止盈止损...")
            
            # 🆕 构建带止盈止损的参数（修复OKX API格式）
            params = {
                'posSide': 'long',
                'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                'slTriggerPx': str(round(validated_sl, 2)),      # 止损触发价格（字符串格式）
                'tpTriggerPx': str(round(validated_tp, 2)),       # 止盈触发价格（字符串格式）
                'slTriggerPxType': 'last',                        # 触发类型：最新成交价
                'tpTriggerPxType': 'last',
                'ordType': 'market'                               # 明确指定订单类型
            }
            
            try:
                print(f"🔧 尝试下单参数: {params}")
                order = exchange.create_market_order(TRADE_CONFIG['symbol'], 'buy', order_amount, None, params)
                print(f"✅ 多单及止盈止损设置成功: {order.get('id', 'N/A')}")
            except Exception as e:
                print(f"❌ 带止盈止损下单失败: {e}")
                print("尝试不带止盈止损下单...")
                # 备用方案：不带止盈止损下单
                basic_params = {
                    'posSide': 'long',
                    'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                    'ordType': 'market'
                }
                try:
                    order = exchange.create_market_order(TRADE_CONFIG['symbol'], 'buy', order_amount, None, basic_params)
                    print(f"✅ 多单下单成功（未设置止盈止损）: {order.get('id', 'N/A')}")
                    
                    # 尝试单独设置止盈止损
                    print("🔄 尝试单独设置止盈止损...")
                    try:
                        # 设置止损
                        sl_order = exchange.create_order(
                            TRADE_CONFIG['symbol'], 'market', 'sell', order_amount, None, {
                                'posSide': 'long',
                                'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                                'ordType': 'conditional',
                                'triggerPx': str(round(validated_sl, 2)),
                                'triggerPxType': 'last',
                                'reduceOnly': True
                            }
                        )
                        print(f"✅ 止损订单设置成功: {sl_order.get('id', 'N/A')}")
                    except Exception as sl_e:
                        print(f"⚠️ 止损订单设置失败: {sl_e}")
                    
                    try:
                        # 设置止盈
                        tp_order = exchange.create_order(
                            TRADE_CONFIG['symbol'], 'market', 'sell', order_amount, None, {
                                'posSide': 'long',
                                'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                                'ordType': 'conditional',
                                'triggerPx': str(round(validated_tp, 2)),
                                'triggerPxType': 'last',
                                'reduceOnly': True
                            }
                        )
                        print(f"✅ 止盈订单设置成功: {tp_order.get('id', 'N/A')}")
                    except Exception as tp_e:
                        print(f"⚠️ 止盈订单设置失败: {tp_e}")
                        
                except Exception as basic_e:
                    print(f"❌ 基础下单也失败: {basic_e}")

        elif signal_data['signal'] == 'SELL':
            side = 'short'
            
            # 🆕 使用智能仓位计算（包含动态杠杆）
            order_amount, dynamic_leverage = calculate_intelligent_position(signal_data, price_data, current_position)
            
            # 🆕 动态设置杠杆（变化显著时才设置，避免触发不必要影响）
            curr_lev = (current_position or {}).get('leverage')
            if curr_lev is None or abs(dynamic_leverage - float(curr_lev)) >= TRADE_THROTTLE['leverage_tol']:
                print(f"🔧 设置动态杠杆: {dynamic_leverage}倍")
                leverage_success = safe_set_leverage(
                    dynamic_leverage,
                    TRADE_CONFIG['symbol'],
                    TRADE_CONFIG.get('td_mode', 'cross')
                )
            else:
                print("杠杆变化不显著，跳过设置杠杆")
                leverage_success = True
            
            if not leverage_success:
                print("⚠️ 杠杆设置失败，使用默认杠杆")
                dynamic_leverage = 5  # 使用默认杠杆
            
            # 获取账户余额进行最终检查
            balance = exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            required_margin = price_data['price'] * order_amount * TRADE_CONFIG['contract_size'] / dynamic_leverage
            
            if required_margin > usdt_balance * 0.8:  # 使用不超过80%的余额
                print(f"⚠️ 保证金不足，跳过交易。需要: {required_margin:.2f} USDT, 可用: {usdt_balance:.2f} USDT")
                return
            
            # 🆕 动态计算止盈止损
            stop_loss_price, take_profit_price = calculate_dynamic_stop_loss_take_profit(
                signal_data, price_data, side, dynamic_leverage
            )
            
            # 🆕 验证止盈止损价格
            is_valid, validated_sl, validated_tp = validate_stop_loss_take_profit(
                {'stop_loss': stop_loss_price, 'take_profit': take_profit_price}, 
                price_data, side
            )
            
            if not is_valid:
                print("❌ 止盈止损验证失败，取消交易")
                return
            
            # 🆕 初始化持仓管理状态
            current_price = price_data['price']
            position_management['current_stop_loss'] = validated_sl
            position_management['initial_stop_loss'] = validated_sl
            position_management['entry_price'] = current_price
            position_management['pyramid_count'] = 0
            position_management['partial_tp_executed'] = {'tp1': False, 'tp2': False, 'tp3': False}
            
            if current_position and current_position['side'] == 'long':
                print("平多仓...")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'], 'sell', current_position['size'],
                    None, {
                        'reduceOnly': True,
                        'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                        'posSide': 'long'
                    }
                )
                time.sleep(2)  # 等待平仓完成
                # 🆕 重置持仓管理状态
                position_management['pyramid_count'] = 0
                position_management['partial_tp_executed'] = {'tp1': False, 'tp2': False, 'tp3': False}
            
            print("开空仓并设置止盈止损...")
            
            # 🆕 构建带止盈止损的参数（修复OKX API格式）
            params = {
                'posSide': 'short',
                'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                'slTriggerPx': str(round(validated_sl, 2)),      # 止损触发价格（字符串格式）
                'tpTriggerPx': str(round(validated_tp, 2)),       # 止盈触发价格（字符串格式）
                'slTriggerPxType': 'last',                        # 触发类型：最新成交价
                'tpTriggerPxType': 'last',
                'ordType': 'market'                               # 明确指定订单类型
            }
            
            try:
                print(f"🔧 尝试下单参数: {params}")
                order = exchange.create_market_order(TRADE_CONFIG['symbol'], 'sell', order_amount, None, params)
                print(f"✅ 空单及止盈止损设置成功: {order.get('id', 'N/A')}")
            except Exception as e:
                print(f"❌ 带止盈止损下单失败: {e}")
                print("尝试不带止盈止损下单...")
                # 备用方案：不带止盈止损下单
                basic_params = {
                    'posSide': 'short',
                    'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                    'ordType': 'market'
                }
                try:
                    order = exchange.create_market_order(TRADE_CONFIG['symbol'], 'sell', order_amount, None, basic_params)
                    print(f"✅ 空单下单成功（未设置止盈止损）: {order.get('id', 'N/A')}")
                    
                    # 尝试单独设置止盈止损
                    print("🔄 尝试单独设置止盈止损...")
                    try:
                        # 设置止损
                        sl_order = exchange.create_order(
                            TRADE_CONFIG['symbol'], 'market', 'buy', order_amount, None, {
                                'posSide': 'short',
                                'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                                'ordType': 'conditional',
                                'triggerPx': str(round(validated_sl, 2)),
                                'triggerPxType': 'last',
                                'reduceOnly': True
                            }
                        )
                        print(f"✅ 止损订单设置成功: {sl_order.get('id', 'N/A')}")
                    except Exception as sl_e:
                        print(f"⚠️ 止损订单设置失败: {sl_e}")
                    
                    try:
                        # 设置止盈
                        tp_order = exchange.create_order(
                            TRADE_CONFIG['symbol'], 'market', 'buy', order_amount, None, {
                                'posSide': 'short',
                                'tdMode': TRADE_CONFIG.get('td_mode', 'cross'),
                                'ordType': 'conditional',
                                'triggerPx': str(round(validated_tp, 2)),
                                'triggerPxType': 'last',
                                'reduceOnly': True
                            }
                        )
                        print(f"✅ 止盈订单设置成功: {tp_order.get('id', 'N/A')}")
                    except Exception as tp_e:
                        print(f"⚠️ 止盈订单设置失败: {tp_e}")
                        
                except Exception as basic_e:
                    print(f"❌ 基础下单也失败: {basic_e}")
        
        elif signal_data['signal'] == 'HOLD':
            print("信号为HOLD，不执行任何交易")
            return  # 🔧 修复：直接返回，不执行任何杠杆相关操作

        print("✅ 订单执行完成!")
        time.sleep(3)
        position = get_current_position()
        print(f"更新后持仓: {position}")
        
        # 🆕 更新持仓管理信息
        if position:
            if position_management.get('entry_price') is None:
                position_management['entry_price'] = position.get('entry_price', price_data['price'])
            if position_management.get('initial_stop_loss') is None:
                position_management['initial_stop_loss'] = position_management.get('current_stop_loss')

        # 成功开仓后，更新节流状态
        try:
            if want_side in ['long', 'short']:
                last_trade_info.update({
                    'timestamp': price_data['timestamp'],
                    'bar_index': curr_bar_index,
                    'side': want_side,
                    'price': current_price,
                    'count_today': last_trade_info.get('count_today', 0) + 1,
                })
        except Exception as _:
            pass

    except Exception as e:
        print(f"❌ 订单执行失败: {e}")
        import traceback
        traceback.print_exc()


def check_stop_loss_take_profit_orders():
    """检查止盈止损订单状态"""
    try:
        # 获取所有开放订单
        orders = exchange.fetch_open_orders(TRADE_CONFIG['symbol'])
        
        stop_orders = []
        for order in orders:
            if order['type'] in ['stop_market', 'take_profit_market']:
                stop_orders.append({
                    'id': order['id'],
                    'type': order['type'],
                    'side': order['side'],
                    'amount': order['amount'],
                    'price': order.get('price', 'N/A'),
                    'status': order['status']
                })
        
        if stop_orders:
            print(f"📋 当前止盈止损订单: {len(stop_orders)}个")
            for order in stop_orders:
                print(f"   - {order['type']}: {order['side']} {order['amount']} @ {order['price']}")
        else:
            print("📋 当前无止盈止损订单")
            
        return stop_orders
        
    except Exception as e:
        print(f"检查止盈止损订单失败: {e}")
        return []


def analyze_with_deepseek_with_retry(price_data, max_retries=2):
    """带重试的DeepSeek分析"""
    
    # 🔴 修复：添加空值检查
    if not price_data or not isinstance(price_data, dict):
        print("❌ price_data 为空或无效，使用备用信号")
        return create_fallback_signal({'price': 0})
    
    for attempt in range(max_retries):
        try:
            signal_data = analyze_with_deepseek(price_data)
            if signal_data and not signal_data.get('is_fallback', False):
                return signal_data

            print(f"第{attempt + 1}次尝试失败，进行重试...")
            time.sleep(1)

        except Exception as e:
            print(f"第{attempt + 1}次尝试异常: {e}")
            import traceback
            print(f"详细错误信息: {traceback.format_exc()}")
            if attempt == max_retries - 1:
                return create_fallback_signal(price_data)
            time.sleep(1)

    return create_fallback_signal(price_data)


def wait_for_next_period():
    """等待到下一个15分钟整点"""
    now = datetime.now()
    current_minute = now.minute
    current_second = now.second

    # 计算下一个整点时间（00, 15, 30, 45分钟）
    next_period_minute = ((current_minute // 15) + 1) * 15
    if next_period_minute == 60:
        next_period_minute = 0

    # 计算需要等待的总秒数
    if next_period_minute > current_minute:
        minutes_to_wait = next_period_minute - current_minute
    else:
        minutes_to_wait = 60 - current_minute + next_period_minute

    seconds_to_wait = minutes_to_wait * 60 - current_second

    # 显示友好的等待时间
    display_minutes = minutes_to_wait - 1 if current_second > 0 else minutes_to_wait
    display_seconds = 60 - current_second if current_second > 0 else 0

    if display_minutes > 0:
        print(f"🕒 等待 {display_minutes} 分 {display_seconds} 秒到整点...")
    else:
        print(f"🕒 等待 {display_seconds} 秒到整点...")

    return seconds_to_wait


def wait_with_progress(seconds):
    """带进度显示的等待函数，保持容器活跃"""
    elapsed = 0
    while elapsed < seconds:
        # 每10秒输出一次进度，保持容器活跃
        time.sleep(10)
        elapsed += 10
        remaining = max(0, seconds - elapsed)
        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            print(f"⏱️  已等待 {elapsed//60} 分钟，还需等待 {mins} 分 {secs} 秒...")
            # 每30秒输出一次心跳，确保Railway知道程序还在运行
            if elapsed % 30 == 0:
                print(f"💓 程序运行正常，等待整点执行交易分析...")
    
    if remaining > 0 and remaining <= 10:
        time.sleep(remaining)  # 等待剩余时间


def trading_bot():
    # 等待到整点再执行
    wait_seconds = wait_for_next_period()
    if wait_seconds > 0:
        wait_with_progress(wait_seconds)

    """主交易机器人函数"""
    print("\n" + "=" * 60)
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. 检查当前止盈止损订单状态
    print("🔍 检查当前止盈止损订单...")
    check_stop_loss_take_profit_orders()

    # 2. 获取增强版K线数据
    price_data = get_btc_ohlcv_enhanced()
    if not price_data:
        return

    print(f"BTC当前价格: ${price_data['price']:,.2f}")
    print(f"数据周期: {TRADE_CONFIG['timeframe']}")
    print(f"价格变化: {price_data['price_change']:+.2f}%")

    # 🆕 检查并管理现有持仓（移动止损、分批止盈）
    current_position = get_current_position()
    if current_position:
        print(f"📊 当前持仓: {current_position['side']} {current_position['size']:.2f} 张, "
              f"盈亏: {current_position['unrealized_pnl']:.2f} USDT")
        
        # 更新持仓管理信息（如果缺失）
        if position_management.get('entry_price') is None:
            position_management['entry_price'] = current_position.get('entry_price', price_data['price'])
        
        # 检查移动止损
        new_sl = update_trailing_stop(current_position, price_data)
        if new_sl:
            try:
                cleanup_stop_loss_orders()
                print(f"🔄 移动止损更新到: {new_sl:.2f}")
            except Exception as e:
                print(f"⚠️ 更新止损失败: {e}")
        
        # 检查分批止盈
        initial_sl = position_management.get('initial_stop_loss')
        if initial_sl:
            execute_partial_take_profit(current_position, price_data, initial_sl)

    # 3. 使用DeepSeek分析（带重试）
    signal_data = analyze_with_deepseek_with_retry(price_data)

    if signal_data.get('is_fallback', False):
        print("⚠️ 使用备用交易信号")

    # 4. 执行交易（集成止盈止损、加仓等）
    execute_trade(signal_data, price_data)
    
    # 5. 交易后再次检查止盈止损订单
    print("🔍 交易后检查止盈止损订单...")
    check_stop_loss_take_profit_orders()


def main():
    """主函数"""
    print("BTC/USDT OKX自动交易机器人启动成功！")
    print("融合技术指标策略 + OKX实盘接口 + 智能止盈止损")

    if TRADE_CONFIG['test_mode']:
        print("当前为模拟模式，不会真实下单")
    else:
        print("实盘交易模式，请谨慎操作！")

    print(f"交易周期: {TRADE_CONFIG['timeframe']}")
    print("已启用完整技术指标分析和持仓跟踪功能")
    print("🆕 已集成智能止盈止损功能：")
    print("   - 动态计算止盈止损点位（基于ATR和动态风险收益比）")
    print("   - 下单时自动设置止盈止损")
    print("   - 动态风险收益比：趋势市1:5，震荡市1:1.5，默认1:3")
    print("   - 根据信心程度和市场波动调整")
    print("🚀 利益最大化优化功能（保守平衡版）：")
    print("   - 资金利用率：70-90%（原25%）")
    print("   - 动态杠杆：2-8倍（保守上限，降低风险）")
    print("   - 移动止损：浮盈5%保本，10%锁3%，20%锁10%")
    print("   - 金字塔加仓：趋势中浮盈5%以上自动加仓")
    print("   - 分批止盈：30%在1.5倍RR，30%在2.5倍RR，40%跟随趋势")
    print("🧠 已优化AI分析能力：")
    print("   - 思维链分析：五步结构化分析")
    print("   - 多空力量对比分析")
    print("   - 关键指标状态评估")
    print("   - 市场结构深度分析")
    print("   - 风险收益综合评估")

    # 设置交易所
    if not setup_exchange():
        print("交易所初始化失败，程序退出")
        return

    print("执行频率: 每15分钟整点执行")
    print("=" * 60)
    print("🚀 程序开始运行，等待整点执行交易分析...")
    print("=" * 60)

    # 循环执行（不使用schedule）
    try:
        while True:
            try:
                trading_bot()  # 函数内部会自己等待整点
                print(f"✅ 本次分析完成，等待下次执行...")
            except Exception as e:
                print(f"❌ 交易机器人执行异常: {e}")
                import traceback
                traceback.print_exc()
                print(f"⏳ 5分钟后重试...")
                time.sleep(300)  # 出错后等待5分钟再重试
            
            # 执行完后等待一段时间再检查（避免频繁循环）
            print(f"🔄 等待下次执行，程序保持运行...")
            time.sleep(60)  # 每分钟检查一次
            
    except KeyboardInterrupt:
        print("\n⚠️ 程序被手动停止")
    except Exception as e:
        print(f"\n❌ 程序异常退出: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()