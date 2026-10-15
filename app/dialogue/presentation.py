"""Представление записи: шаблоны Ильи, факты backend и короткие реплики."""

from app.clock import CLINIC_TIMEZONE
from app.contracts import DialogOption
from app.voice.render import RenderError, default_renderer, plain, say_digits


class BookingPresenter:
    def __init__(self, clock):
        self.clock = clock
        self.renderer = default_renderer()

    def _today(self):
        return self.clock.now().astimezone(CLINIC_TIMEZONE).date()

    def ask_patient(self):
        return self.renderer.render("ident.ask_patient", self._today()).text

    def offer(self, slots, doctors):
        options = tuple(
            DialogOption(
                id=f"slot-{s.doctor_id}-{s.scheduled_at:%Y%m%d%H%M}",
                day=s.scheduled_at.date(),
                at=s.scheduled_at.time(),
                doctor=doctors[s.doctor_id].full_name,
                specialty=s.specialty,
            )
            for s in slots
        )
        count = self.renderer.fitting_options("booking.offer", self._today(), options)
        if count:
            spoken = self.renderer.render(
                "booking.offer", self._today(), options=options[:count]
            ).text
            return tuple(slots[:count]), (spoken,)
        # Сохраняем все факты даже для очень длинного ФИО; не выдумываем сокращение.
        spoken = say_digits(
            f"Первый вариант: {slots[0].scheduled_at:%d.%m.%Y %H:%M}, {doctors[slots[0].doctor_id].full_name}.",
            self._today(),
        )
        return tuple(slots[:1]), self._short(spoken) + ("Подходит этот вариант?",)

    def confirmation(self, slot, doctor_name, room):
        values = self._values(slot, doctor_name, room)
        template = "booking.confirm" if room else "booking.confirm_without_room"
        try:
            return (self.renderer.render(template, self._today(), **values).text,)
        except RenderError:
            details = say_digits(
                f"Подтвердите: {slot.specialty}, {doctor_name}, {slot.scheduled_at:%d.%m.%Y %H:%M}"
                + (f", кабинет {room}." if room else "."),
                self._today(),
            )
            return self._short(details) + ("Всё верно?",)

    def completed(self, slot, doctor_name, room):
        template = "booking.result" if room else "booking.result_without_room"
        try:
            return self.renderer.render(
                template, self._today(), **self._values(slot, doctor_name, room)
            ).text
        except RenderError:
            # Короткий результат не обещает SMS: доставка ещё не подключена.
            return "Запись подтверждена. Данные приёма сохранены."

    @staticmethod
    def _values(slot, doctor_name, room):
        return {
            "specialty": slot.specialty,
            "doctor": doctor_name or "врач",
            "date": slot.scheduled_at.date(),
            "time": slot.scheduled_at.time(),
            "room": room,
        }

    @staticmethod
    def _short(text):
        words = plain(text).split()
        return tuple(" ".join(words[i : i + 20]) for i in range(0, len(words), 20))
