import pjsua2 as pj
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class RealTimeCall(pj.Call):
    def __init__(self, acc, call_id=-1):
        pj.Call.__init__(self, acc, call_id)
        self.connected = False

    def onCallState(self, prm):
        ci = self.getInfo()
        logger.info(f"📞 Статус звонка: {ci.stateText}")

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected = True
            logger.info("✅ Звонок принят! Разговор начался...")
            logger.info("🎙️ Говорите в микрофон — слушайте в динамики!")
            # Подключаем аудиоустройства к медиа потоку
            self.connect_audio_devices()

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.connected = False
            logger.info("❌ Звонок завершен")

    def onCallMediaState(self, prm):
        """Callback при изменении состояния медиа потока"""
        logger.info("🎵 Медиа поток активирован")
        if self.connected:
            self.connect_audio_devices()

    def connect_audio_devices(self):
        """Подключение микрофона и динамиков к медиа потоку"""
        try:
            # Получаем аудио медиа звонка
            call_aud_med = self.getAudioMedia(0)  # Индекс аудио потока (обычно 0)

            # Получаем медиа-порт микрофона (устройство записи)
            aud_mgr = pj.Endpoint.instance().audDevManager()
            mic_med = aud_mgr.getCaptureDevMedia()

            # Подключаем микрофон к медиа потоку (передача вашего голоса)
            mic_med.startTransmit(call_aud_med)
            logger.info("🎤 Микрофон подключен к звонку (ваш голос передаётся)")

            # Подключаем медиа поток к динамикам (воспроизведение голоса собеседника)
            speaker_med = aud_mgr.getPlaybackDevMedia()
            call_aud_med.startTransmit(speaker_med)
            logger.info("🔈 Динамики подключены к звонку (голос собеседника слышен)")

            logger.info("🎉 Реал-таймовый разговор активирован!")
            logger.info("🗣️ ГОВОРИТЕ И СЛУШАЙТЕ!")

        except Exception as e:
            logger.error(f"❌ Ошибка подключения аудиоустройств: {e}")


def voip_realtime_call():
    """VoIP звонок с подключением микрофона и динамиков"""
    ep = None
    call = None

    try:
        logger.info("=== VOIP РЕАЛ-ТАЙМОВЫЙ ЗВОНОК ===")

        # Инициализация endpoint
        ep = pj.Endpoint()
        ep.libCreate()

        # Конфигурация
        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.maxCalls = 2
        ep_cfg.medConfig.sndClockRate = 8000  # Частота звука
        ep_cfg.medConfig.audioFramePtime = 20  # Размер фрейма

        ep.libInit(ep_cfg)

        # Настройка кодеков (желательно использовать PCMU/PCMA)
        codec_list = [
            ("PCMU/8000", 255),
            ("PCMA/8000", 254),
            ("GSM/8000", 0),
            ("speex/8000", 0),
            ("speex/16000", 0),
            ("speex/32000", 0),
            ("iLBC/8000", 0),
            ("opus/48000", 0),  # Отключаем Opus, если не нужен
        ]
        for codec_name, priority in codec_list:
            try:
                ep.codecSetPriority(codec_name, priority)
            except Exception as e:
                logger.debug(f"Кодек {codec_name} не найден: {e}")

        # Настройка аудиоустройств
        try:
            aud_mgr = ep.audDevManager()
            # Обновляем список устройств
            aud_mgr.refreshDevs()

            # Получаем список устройств
            dev_count = aud_mgr.getDevCount()
            logger.info(f"🔍 Найдено аудиоустройств: {dev_count}")

            # Ищем подходящее полнодуплексное устройство
            found_device = False
            for i in range(dev_count):
                dev_info = aud_mgr.getDevInfo(i)
                logger.debug(f"Device {i}: {dev_info.name}, in={dev_info.inputCount}, out={dev_info.outputCount}")
                if dev_info.inputCount > 0 and dev_info.outputCount > 0:
                    logger.info(f"🎯 Найдено полнодуплексное устройство: {dev_info.name} (ID: {i})")
                    aud_mgr.setCaptureDev(i)
                    aud_mgr.setPlaybackDev(i)
                    found_device = True
                    break

            if not found_device:
                logger.warning("⚠ Полнофункциональное аудиоустройство не найдено, используем default")
                aud_mgr.setCaptureDev(-1)  # default
                aud_mgr.setPlaybackDev(-1)  # default

            # Проверяем, какие устройства установлены
            cap_dev = aud_mgr.getCaptureDev()
            play_dev = aud_mgr.getPlaybackDev()
            logger.info(f"📋 Устройства: capture={cap_dev}, playback={play_dev}")

        except Exception as e:
            logger.error(f"❌ Ошибка настройки аудиоустройств: {e}")
            return

        # Транспорт
        tp_cfg = pj.TransportConfig()
        tp_cfg.port = 5060
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp_cfg)

        ep.libStart()

        # Аккаунт
        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = "sip:9000@192.168.128.22:5061"
        acc_cfg.regConfig.registrarUri = "sip:192.168.128.22:5061"
        cred = pj.AuthCredInfo("digest", "asterisk", "9000", 0, "3d12d14b415b5b8b2667820156c0a306")
        acc_cfg.sipConfig.authCreds.append(cred)

        acc = pj.Account()
        acc.create(acc_cfg)

        # Ждем регистрации
        logger.info("⏳ Регистрация...")
        time.sleep(3)

        # Совершаем звонок
        logger.info("📞 Набираем 539...")
        call_prm = pj.CallOpParam()
        call_prm.opt.audioCount = 1
        call_prm.opt.videoCount = 0

        call = RealTimeCall(acc)
        call.makeCall("sip:539@192.168.128.22:5061", call_prm)

        # Ожидание разговора
        logger.info("🕐 Ожидание ответа и соединения...")

        call_answered = False
        max_wait = 30  # секунд

        for i in range(max_wait):
            if not call:
                break

            try:
                call_info = call.getInfo()

                if i % 5 == 0:
                    logger.info(f"📊 Статус: {call_info.stateText} ({i}с)")

                if call_info.state == pj.PJSIP_INV_STATE_CONFIRMED and not call_answered:
                    call_answered = True
                    logger.info("🎉 СОЕДИНЕНИЕ УСТАНОВЛЕНО!")
                    logger.info("🗣️ ГОВОРИТЕ В МИКРОФОН — СЛУШАЙТЕ В ДИНАМИКИ!")

                elif call_info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                    logger.info("📞 Звонок завершен удаленной стороной")
                    break

                # Ждем 25 секунд, чтобы дать время поговорить
                if call_answered and i >= 25:
                    logger.info("⏰ Время разговора истекло. Завершаем...")
                    break

            except Exception as e:
                logger.debug(f"Ошибка статуса: {e}")

            time.sleep(1)

        # Завершение звонка
        if call and hasattr(call, 'connected') and call.connected:
            try:
                logger.info("📞 Завершаем звонок...")
                call.hangup(pj.CallOpParam())
            except:
                pass

        time.sleep(2)

    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        import traceback
        logger.error(traceback.format_exc())

    finally:
        if ep:
            try:
                logger.info("🛑 Завершение библиотеки...")
                ep.libDestroy()
                logger.info("✅ Библиотека завершена")
            except:
                pass

        logger.info("✅ Программа завершена")


if __name__ == "__main__":
    voip_realtime_call()