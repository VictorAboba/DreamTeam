"""Unique human names for swarm members."""

from __future__ import annotations

import random

EN_FIRST = [
    "John", "Emma", "Liam", "Olivia", "Noah", "Ava", "James", "Sophia", "Lucas", "Mia", "Henry", "Grace",
    "Oliver", "Chloe", "Ethan", "Lily", "Daniel", "Zoe", "Samuel", "Nora", "David", "Hannah", "Leo", "Ruby",
    "Isaac", "Clara", "Owen", "Alice", "Jack", "Ella", "Adam", "Iris", "Felix", "Maya", "Hugo", "Eva",
]
EN_LAST = [
    "Smith", "Johnson", "Brown", "Taylor", "Anderson", "Thomas", "Moore", "Martin", "Clark", "Lewis",
    "Walker", "Hall", "Young", "King", "Wright", "Scott", "Green", "Baker", "Adams", "Nelson", "Carter",
    "Mitchell", "Roberts", "Turner", "Phillips", "Campbell", "Parker", "Evans", "Collins", "Stewart",
    "Morris", "Cooper", "Reed", "Bailey", "Bell", "Murphy", "Rivera", "Ward", "Brooks", "Foster",
]
RU_MALE = [
    "Алексей", "Иван", "Дмитрий", "Сергей", "Андрей", "Михаил", "Николай", "Павел", "Егор", "Артём",
    "Максим", "Кирилл", "Олег", "Роман", "Фёдор", "Григорий", "Тимур", "Владимир", "Борис", "Лев",
]
RU_FEMALE = [
    "Анна", "Мария", "Елена", "Ольга", "Наталья", "Ирина", "Татьяна", "Светлана", "Дарья", "Полина",
    "Екатерина", "Алиса", "Вера", "Ксения", "Юлия", "Софья", "Марина", "Василиса", "Ника", "Варвара",
]
RU_SURNAMES_MALE = [
    "Иванов", "Смирнов", "Кузнецов", "Попов", "Соколов", "Лебедев", "Козлов", "Новиков", "Морозов",
    "Волков", "Соловьёв", "Васильев", "Зайцев", "Павлов", "Семёнов", "Голубев", "Виноградов", "Богданов",
    "Воробьёв", "Фёдоров", "Михайлов", "Беляев", "Тарасов", "Белов", "Комаров", "Орлов", "Киселёв",
    "Макаров", "Андреев", "Ковалёв", "Ильин", "Гусев", "Титов", "Кузьмин", "Кудрявцев", "Баранов",
    "Куликов", "Алексеев", "Степанов", "Яковлев", "Сорокин", "Сергеев", "Романов", "Захаров", "Борисов",
    "Королёв", "Герасимов", "Пономарёв", "Григорьев", "Лазарев", "Медведев", "Ершов", "Никитин",
    "Соболев", "Рябов", "Поляков", "Цветков", "Данилов", "Жуков", "Фролов", "Журавлёв", "Николаев",
    "Крылов", "Максимов", "Сидоров", "Осипов", "Белоусов", "Федотов", "Дорофеев", "Егоров", "Матвеев",
    "Бобров", "Дмитриев", "Калинин", "Анисимов", "Петухов", "Антонов", "Тимофеев", "Никифоров", "Веселов",
]


def _feminine(surname: str) -> str:
    if surname.endswith(("ов", "ёв", "ев", "ин", "ын")):
        return surname + "а"
    if surname.endswith("ий"):
        return surname[:-2] + "ая"
    return surname


def candidate(language: str, rng: random.Random) -> str:
    if language == "ru":
        surname = rng.choice(RU_SURNAMES_MALE)
        if rng.random() < 0.5:
            return f"{rng.choice(RU_MALE)} {surname}"
        return f"{rng.choice(RU_FEMALE)} {_feminine(surname)}"
    return f"{rng.choice(EN_FIRST)} {rng.choice(EN_LAST)}"


def generate_name(taken: set[str], language: str = "en", rng: random.Random | None = None) -> str:
    """Return a human name that is not in `taken` (case-insensitive) and whose first name is not reused
    while there are fresh first names left, so agents are easy to tell apart in chat."""
    rng = rng or random.Random()
    taken_lower = {t.lower() for t in taken}
    first_names = {t.split()[0].lower() for t in taken if t}
    for strict in (True, False):
        for _ in range(500):
            name = candidate(language, rng)
            if name.lower() in taken_lower:
                continue
            if strict and name.split()[0].lower() in first_names:
                continue
            return name
    n = 2
    while True:
        name = f"{candidate(language, rng)} {n}"
        if name.lower() not in taken_lower:
            return name
        n += 1
