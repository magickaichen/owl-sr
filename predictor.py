from collections import defaultdict, deque, OrderedDict
from functools import cmp_to_key
from itertools import chain
from math import log, sqrt
from random import choices, random
from typing import Dict, List, Sequence, Set, Tuple

import numpy as np
from scipy.optimize import fmin
from trueskill import calc_draw_margin, Rating, TrueSkill

from game import Roster, Game
from fetcher import (Availabilities,
                     load_availabilities,
                     load_games,
                     save_ratings_history)


PScores = Dict[Tuple[int, int], float]


class Predictor(object):
    """Base class for all OWL predictors."""

    def __init__(self, availabilities: Availabilities = None,
                 roster_queue_size: int = 12) -> None:
        super().__init__()

        # Players availabilities.
        if availabilities is None:
            availabilities = load_availabilities()
        self.availabilities = availabilities

        # Track recent used rosters.
        self.roster_queues = defaultdict(
            lambda: deque(maxlen=roster_queue_size))

        # Global standings.
        self.map_diffs = defaultdict(int)
        self.head_to_head_map_diffs = defaultdict(int)

        # Stage standings.
        self.stage = None
        self.base_stage = None

        self.stage_wins = defaultdict(int)
        self.stage_losses = defaultdict(int)
        self.stage_map_diffs = defaultdict(int)
        self.stage_head_to_head_map_diffs = defaultdict(int)
        self.stage_title_wins = defaultdict(int)
        self.stage_title_losses = defaultdict(int)

        # Match standings.
        self.match_id = None
        self.score = None
        self.scores = defaultdict(dict)
        # stage => {team: [match_id]}
        self.match_history = defaultdict(lambda: defaultdict(list))

        # Draw counts, used to adjust parameters related to draws.
        self.expected_draws = 0.0
        self.real_draws = 0.0

        # Evaluation history, used to judge the performance of a predictor.
        self.points = []
        self.corrects = []

    @property
    def stage_finished(self):
        return sum(self.stage_title_losses.values()) == 2

    def _train(self, game: Game) -> None:
        """Given a game result, train the underlying model."""
        raise NotImplementedError

    def predict(self, teams: Tuple[str, str],
                rosters: Tuple[Roster, Roster] = None,
                drawable: bool = False) -> Tuple[float, float]:
        """Given two teams, return win/draw probabilities of them."""
        raise NotImplementedError

    def train(self, game: Game) -> float:
        """Given a game result, train the underlying model.
        Return the prediction point for this game before training."""
        point, correct = self.evaluate(game)
        self.points.append(point)
        self.corrects.append(correct)

        self._update_rosters(game)
        self._update_standings(game)
        self._update_draws(game)

        self._train(game)

        return point

    def evaluate(self, game: Game) -> Tuple[float, bool]:
        """Return the prediction point for this game.
        Assume it will not draw."""
        if game.score[0] == game.score[1]:
            return 0.0, False

        p_win, p_draw = self.predict(game.teams, game.rosters, drawable=False)
        p_win = max(0.0, min(p_win, 1.0))
        p_draw = max(0.0, min(p_draw, 1.0))
        p_loss = 1.0 - p_win - p_draw

        if game.score[0] > game.score[1]:
            p = p_win
            correct = p_win > p_loss
        else:
            p = p_loss
            correct = p_win < p_loss

        return log(2.0 * p), correct

    def train_games(self, games: Sequence[Game]) -> float:
        """Given a sequence of games, train the underlying model.
        Return the prediction point for all the games."""
        total_point = 0.0

        for game in games:
            point = self.train(game)
            total_point += point

        return total_point

    def predict_match_score(
            self, teams: Tuple[str, str],
            rosters: Tuple[Roster, Roster] = None,
            match_format: str = 'regular') -> PScores:
        """Predict the scores of a given match."""
        if match_format == 'regular':
            drawables = [True, False, True, False]
            return self._predict_bo_match_score(teams, rosters,
                                                drawables=drawables)
        elif match_format == 'title':
            drawables = [False, False, True, True, False]
            return self._predict_bo_match_score(teams, rosters,
                                                drawables=drawables)
        else:
            raise NotImplementedError

    def predict_match(
            self, teams: Tuple[str, str],
            rosters: Tuple[Roster, Roster] = None,
            match_format: str = 'regular') -> Tuple[float, float]:
        """Predict the win probability & diff expectation of a given match."""
        p_scores = self.predict_match_score(teams, rosters,
                                            match_format=match_format)
        p_win = 0.0
        e_diff = 0.0

        for (score1, score2), p in p_scores.items():
            if score1 > score2:
                p_win += p
            e_diff += p * (score1 - score2)

        return p_win, e_diff

    def predict_stage(self, games: Sequence[Game]):
        games = [game for game in games if game.stage == self.stage and
                 game.match_format == 'regular']
        prediction = self._predict_stage(games)
        teams = list(prediction.keys())

        # Normalize 0% and 100% for predictions.
        wins = {team: (self.stage_wins[team], self.stage_map_diffs[team])
                for team in teams}
        min_wins = wins.copy()
        max_wins = wins.copy()

        for game in games:
            for team in game.teams:
                win, map_diff = min_wins[team]
                min_wins[team] = (win, map_diff - 4)

                win, map_diff = max_wins[team]
                max_wins[team] = (win + 1, map_diff + 4)

        min_3rd_wins = list(sorted(min_wins.values()))[-3]
        max_4th_wins = list(sorted(max_wins.values()))[-4]

        for team, (p_top3, p_top1) in prediction.items():
            if max_wins[team] < min_3rd_wins:
                p_top3 = False
                p_top1 = False
            elif min_wins[team] > max_4th_wins:
                p_top3 = True

                if self.stage_title_losses[team] > 0:
                    p_top1 = False
                elif self.stage_finished:
                    p_top1 = True

            prediction[team] = (p_top3, p_top1)

        return prediction

    def _predict_stage(self, games: Sequence[Game], iters=100000):
        teams = self._stage_teams()

        scores_list, cum_weights_list = self._games_scores_cum_weights(games)
        p_wins_regular = self._p_wins(teams, match_format='regular')
        p_wins_title = self._p_wins(teams, match_format='title')

        top3_count = {team: 0 for team in teams}
        top1_count = {team: 0 for team in teams}

        for i in range(iters):
            wins = self.stage_wins.copy()
            map_diffs = self.stage_map_diffs.copy()
            head_to_head_map_diffs = self.stage_head_to_head_map_diffs.copy()

            for game, scores, cum_weights in zip(games,
                                                 scores_list,
                                                 cum_weights_list):
                team1, team2 = game.teams
                score1, score2 = choices(scores, cum_weights=cum_weights)[0]

                if score1 > score2:
                    wins[team1] += 1
                elif score1 < score2:
                    wins[team2] += 1

                map_diff = score1 - score2
                map_diffs[team1] += map_diff
                map_diffs[team2] -= map_diff

                head_to_head_map_diffs[(team1, team2)] += map_diff
                head_to_head_map_diffs[(team2, team1)] -= map_diff

            # Determine top 3 teams.
            top3 = self._top3_teams(teams, wins, map_diffs,
                                    head_to_head_map_diffs, p_wins_regular)
            for team in top3:
                top3_count[team] += 1

            # Determine top 1 teams.
            first, second, third = top3

            if self.stage_title_wins[second] > 0:
                pass
            elif self.stage_title_wins[third] > 0:
                second = third
            elif random() < p_wins_title[(third, second)]:
                second = third

            if self.stage_title_wins[first] > 0:
                pass
            elif self.stage_title_wins[second] > 1:
                first = second
            elif random() < p_wins_title[(second, first)]:
                first = second

            top1_count[first] += 1

        return {team: (top3_count[team] / iters, top1_count[team] / iters)
                for team in teams}

    def _predict_bo_match_score(self, teams: Tuple[str, str],
                                rosters: Tuple[Roster, Roster],
                                drawables: List[bool]) -> PScores:
        """Predict the scores of a given BO match."""
        p_scores = defaultdict(float)
        p_scores[(0, 0)] = 1.0

        p_undrawable = self.predict(teams, rosters, drawable=False)
        p_drawable = self.predict(teams, rosters, drawable=True)

        for drawable in drawables:
            p_win, p_draw = p_drawable if drawable else p_undrawable
            p_loss = 1.0 - p_win - p_draw
            new_p_scores = defaultdict(float)

            for (score1, score2), p in p_scores.items():
                new_p_scores[(score1 + 1, score2)] += p * p_win
                new_p_scores[(score1, score2 + 1)] += p * p_loss
                if drawable:
                    new_p_scores[(score1, score2)] += p * p_draw

            p_scores = new_p_scores

        # Add a tie-breaker game if needed.
        p_win, p_draw = p_undrawable
        new_p_scores = defaultdict(float)

        for (score1, score2), p in p_scores.items():
            if score1 == score2:
                new_p_scores[(score1 + 1, score2)] += p * p_win
                new_p_scores[(score1, score2 + 1)] += p * p_loss
            else:
                new_p_scores[(score1, score2)] += p

        p_scores = new_p_scores

        return p_scores

    def _update_rosters(self, game: Game) -> None:
        for team, roster in zip(game.teams, game.rosters):
            self.roster_queues[team].appendleft(roster)

    def _update_stage(self, stage: str) -> None:
        if stage != self.stage:
            if self.stage is not None and stage.startswith(self.stage):
                # Entering the title matches.
                self.base_stage = self.stage
                self.stage = stage
            else:
                # Entering a new stage.
                self.base_stage = stage
                self.stage = stage

                self.stage_wins.clear()
                self.stage_losses.clear()
                self.stage_map_diffs.clear()
                self.stage_head_to_head_map_diffs.clear()
                self.stage_title_wins.clear()
                self.stage_title_losses.clear()

    def _update_match_ids(self, match_id: int, teams: Tuple[str, str]) -> None:
        if match_id != self.match_id:
            # Record a new match.
            self.match_id = match_id
            self.score = {team: 0 for team in teams}
            self.scores[match_id] = self.score

            for team in teams:
                self.match_history[self.stage][team].append(match_id)

    def _update_standings(self, game: Game) -> None:
        self._update_stage(game.stage)
        self._update_match_ids(game.match_id, game.teams)

        # Wins & map diffs.
        team1, team2 = game.teams
        score1, score2 = game.score
        is_title = game.match_format == 'title'

        if game.score[0] != game.score[1]:
            if game.score[0] > game.score[1]:
                winner, loser = game.teams
            else:
                loser, winner = game.teams

            self.map_diffs[winner] += 1
            self.map_diffs[loser] -= 1
            self.head_to_head_map_diffs[(winner, loser)] += 1
            self.head_to_head_map_diffs[(loser, winner)] -= 1

            if not is_title:
                self.stage_map_diffs[winner] += 1
                self.stage_map_diffs[loser] -= 1
                self.stage_head_to_head_map_diffs[(winner, loser)] += 1
                self.stage_head_to_head_map_diffs[(loser, winner)] -= 1

            # Handle the match result.
            if self.score[winner] == self.score[loser]:
                # The winner won the match.
                if is_title:
                    self.stage_title_wins[winner] += 1
                    self.stage_title_losses[loser] += 1
                else:
                    self.stage_wins[winner] += 1
                    self.stage_losses[loser] += 1
            elif self.score[winner] == self.score[loser] - 1:
                # The winner avoided the loss.
                if is_title:
                    self.stage_title_wins[loser] -= 1
                    self.stage_title_losses[winner] -= 1
                else:
                    self.stage_wins[loser] -= 1
                    self.stage_losses[winner] -= 1

            self.score[winner] += 1

    def _update_draws(self, game: Game) -> None:
        if game.drawable:
            _, p_draw = self.predict(game.teams, game.rosters, drawable=True)
            self.expected_draws += p_draw
        if game.score[0] == game.score[1]:
            self.real_draws += 1.0

    def _stage_teams(self) -> Set[str]:
        teams = set()
        for (stage, _), team_members in self.availabilities.items():
            if stage != self.stage:
                continue
            teams.update(team_members.keys())

        return teams

    def _games_scores_cum_weights(self, games: Sequence[Game]):
        scores_list = []
        cum_weights_list = []

        for game in games:
            if game.stage != self.stage:
                continue

            p_scores = self.predict_match_score(game.teams)
            scores = []
            cum_weights = []
            cum_weight = 0.0

            for score, p in p_scores.items():
                scores.append(score)
                cum_weight += p
                cum_weights.append(cum_weight)

            scores_list.append(scores)
            cum_weights_list.append(cum_weights)

        return scores_list, cum_weights_list

    def _p_wins(self, teams: Sequence[str], match_format: str):
        p_wins = {}

        for team1 in teams:
            for team2 in teams:
                team_pair = (team1, team2)
                p_win, _ = self.predict_match(team_pair,
                                              match_format=match_format)
                p_wins[team_pair] = p_win

        return p_wins

    def _top3_teams(self, teams, wins, map_diffs, head_to_head_map_diffs,
                    p_wins_regular):
        def cmp_team(team1, team2):
            if wins[team1] < wins[team2]:
                return -1
            elif wins[team1] > wins[team2]:
                return 1
            elif map_diffs[team1] < map_diffs[team2]:
                return -1
            elif map_diffs[team1] > map_diffs[team2]:
                return 1
            elif head_to_head_map_diffs[(team1, team2)] < 0:
                return -1
            elif head_to_head_map_diffs[(team1, team2)] > 0:
                return 1
            elif random() < p_wins_regular[(team1, team2)]:
                return 1
            else:
                return -1

        teams = list(sorted(teams, key=cmp_to_key(cmp_team), reverse=True))
        return teams[:3]


class SimplePredictor(Predictor):
    """A simple predictor based on map differentials."""

    def __init__(self, alpha: float = 0.2, beta: float = 0.0, **kws) -> None:
        super().__init__(**kws)

        self.alpha = alpha
        self.beta = beta

    def _train(self, game: Game) -> None:
        """Given a game result, train the underlying model.
        Return the prediction point for this game before training."""
        pass

    def predict(self, teams: Tuple[str, str],
                rosters: Tuple[Roster, Roster] = None,
                drawable: bool = False) -> Tuple[float, float]:
        """Given two teams, return win/draw probabilities of them."""
        team1, team2 = teams
        diff1 = self.map_diffs[team1]
        diff2 = self.map_diffs[team2]

        if diff1 > diff2:
            p_win = 0.5 + self.alpha
        elif diff1 == diff2:
            record = self.head_to_head_map_diffs[teams]
            if record > 0:
                p_win = 0.5 + self.beta
            elif record == 0:
                p_win = 0.5
            else:
                p_win = 0.5 - self.beta
        else:
            p_win = 0.5 - self.alpha

        return p_win, 0.0


class TrueSkillPredictor(Predictor):
    """Team-based TrueSkill predictor."""

    def __init__(self, mu: float = 2500.0, sigma: float = 2500.0 / 3.0,
                 beta: float = 2500.0 / 2.0, tau: float = 25.0 / 3.0,
                 draw_probability: float = 0.06, **kws) -> None:
        super().__init__(**kws)

        self.env_drawable = TrueSkill(mu=mu, sigma=sigma, beta=beta, tau=tau,
                                      draw_probability=draw_probability)
        self.env_undrawable = TrueSkill(mu=mu, sigma=sigma, beta=beta, tau=tau,
                                        draw_probability=0.0)
        self.ratings = self._create_rating_jar()

    def _train(self, game: Game) -> None:
        """Given a game result, train the underlying model.
        Return the prediction point for this game before training."""
        score1, score2 = game.score
        if score1 > score2:
            ranks = [0, 1]  # Team 1 wins.
        elif score1 == score2:
            ranks = [0, 0]  # Draw.
        else:
            ranks = [1, 0]  # Team 2 wins.

        env = self.env_drawable if game.drawable else self.env_undrawable
        teams_ratings = env.rate(self._teams_ratings(game.teams, game.rosters),
                                 ranks=ranks)
        self._update_teams_ratings(game, teams_ratings)

    def predict(self, teams: Tuple[str, str],
                rosters: Tuple[Roster, Roster] = None,
                drawable: bool = False) -> Tuple[float, float]:
        """Given two teams, return win/draw probabilities of them."""
        env = self.env_drawable if drawable else self.env_undrawable

        team1_ratings, team2_ratings = self._teams_ratings(teams, rosters)
        size = len(team1_ratings) + len(team2_ratings)

        delta_mu = (sum(r.mu for r in team1_ratings) -
                    sum(r.mu for r in team2_ratings))
        draw_margin = calc_draw_margin(env.draw_probability, size, env=env)
        sum_sigma = sum(r.sigma**2 for r in chain(team1_ratings,
                                                  team2_ratings))
        denom = sqrt(size * env.beta**2 + sum_sigma)

        p_win = env.cdf((delta_mu - draw_margin) / denom)
        p_not_loss = env.cdf((delta_mu + draw_margin) / denom)

        return p_win, p_not_loss - p_win

    def _teams_ratings(self, teams: Tuple[str, str],
                       rosters: Tuple[Roster, Roster]):
        return [self.ratings[teams[0]]], [self.ratings[teams[1]]]

    def _update_teams_ratings(self, game: Game, teams_ratings):
        for team, ratings in zip(game.teams, teams_ratings):
            self.ratings[team] = ratings[0]

    def _create_rating_jar(self):
        return defaultdict(lambda: self.env_drawable.create_rating())


class PlayerTrueSkillPredictor(TrueSkillPredictor):
    """Player-based TrueSkill predictor. Guess the rosters based on history
    when the rosters are not provided."""

    def __init__(self, **kws):
        super().__init__(**kws)

        self.best_rosters = {}
        self.ratings_history = OrderedDict()

    def save_ratings_history(self):
        save_ratings_history(self.ratings_history,
                             mu=self.env_drawable.mu,
                             sigma=self.env_drawable.sigma)

    def _teams_ratings(self, teams: Tuple[str, str],
                       rosters: Tuple[Roster, Roster]):
        if rosters is None:
            # No rosters provided, use the best rosters if possible.
            rosters = [self.best_rosters.get(team, [''] * 6) for team in teams]

        return ([self.ratings[name] for name in rosters[0]],
                [self.ratings[name] for name in rosters[1]])

    def _update_teams_ratings(self, game: Game, teams_ratings) -> None:
        for team, roster, ratings in zip(game.teams, game.rosters,
                                         teams_ratings):
            for name, rating in zip(roster, ratings):
                self.ratings[name] = rating

            self.ratings[team] = self._record_team_ratings(team)

    def _record_team_ratings(self, team: str) -> Rating:
        match_number = len(self.match_history[self.stage][team])
        match_key = (self.stage, match_number)

        members = self.availabilities[match_key][team]
        if match_key not in self.ratings_history:
            self.ratings_history[match_key] = {}
        ratings = self.ratings_history[match_key]

        # Record player ratings.
        for name in members:
            ratings[name] = self.ratings[name]

        # Update the best roster.
        best_roster = self._update_best_roster(team, members)

        # Record the team rating.
        rating = self._roster_rating(best_roster)
        ratings[team] = rating
        return rating

    def _update_best_roster(self, team: str, members: Set[str]):
        rosters = sorted(self.roster_queues[team],
                         key=lambda roster: self._min_roster_rating(roster),
                         reverse=True)
        best_roster = None

        for roster in rosters:
            if all(name in members for name in roster):
                best_roster = roster
                break

        if best_roster is None:
            # Just pick the best 6.
            sorted_members = sorted(members,
                                    key=lambda name: self._min_rating(name),
                                    reverse=True)
            best_roster = tuple(sorted_members[:6])

        self.best_rosters[team] = best_roster
        return best_roster

    def _roster_rating(self, roster: Roster) -> Tuple[float, float]:
        sum_mu = sum(self.ratings[name].mu for name in roster)
        sum_sigma = sqrt(sum(self.ratings[name].sigma**2 for name in roster))

        mu = sum_mu / 6.0
        sigma = sum_sigma / 6.0
        return Rating(mu=mu, sigma=sigma)

    def _min_roster_rating(self, roster: Roster) -> float:
        rating = self._roster_rating(roster)
        return rating.mu - 3.0 * rating.sigma

    def _min_rating(self, name: str) -> float:
        rating = self.ratings[name]
        return rating.mu - 3.0 * rating.sigma


def optimize_beta(class_=PlayerTrueSkillPredictor, maxfun=100) -> None:
    games, _ = load_games()

    def f(x):
        predictor = class_(beta=x[0])
        return -predictor.train_games(games)

    args = fmin(f, [2500.0 / 6.0], maxfun=maxfun)
    avg_point = -f(args) / len(games)
    print(f'beta = {args[0]:.0f}, avg(point) = {avg_point:.4f}')


def optimize_draw_probability(class_=PlayerTrueSkillPredictor,
                              maxfun=100) -> None:
    games, _ = load_games()

    def f(x):
        predictor = class_(draw_probability=x[0])
        predictor.train_games(games)
        return (predictor.expected_draws - predictor.real_draws)**2

    args = fmin(f, [0.06], maxfun=maxfun)
    print(f'draw_probability = {args[0]:.3f}')


def compare_methods() -> None:
    games, _ = load_games()
    classes = [
        SimplePredictor,
        TrueSkillPredictor,
        PlayerTrueSkillPredictor
    ]

    for class_ in classes:
        predictor = class_()
        avg_point = predictor.train_games(games) / len(games)
        avg_accuracy = np.sum(np.array(predictor.corrects)) / len(games)
        print(f'{class_.__name__:>30} {avg_point:8.4f} {avg_accuracy:7.3f}')


def predict_stage():
    past_games, future_games = load_games()

    predictor = PlayerTrueSkillPredictor()
    predictor.train_games(past_games)

    p_stage = predictor.predict_stage(future_games)
    teams = sorted(p_stage.keys(), key=lambda team: p_stage[team][-1],
                   reverse=True)

    print(predictor.base_stage)
    print(f'       Top3   Top1  Roster')
    for team in teams:
        p_top3, p_top1 = p_stage[team]

        if isinstance(p_top3, bool):
            top3 = str(p_top3)
        else:
            top3 = f'{round(p_top3 * 100)}%'

        if isinstance(p_top1, bool):
            top1 = str(p_top1)
        else:
            top1 = f'{round(p_top1 * 100)}%'

        roster = ' '.join(predictor.best_rosters[team])

        print(f'{team:>4}  {top3:>5}  {top1:>5}  {roster}')


def save_ratings():
    past_games, future_games = load_games()

    predictor = PlayerTrueSkillPredictor()
    predictor.train_games(past_games)
    predictor.save_ratings_history()


if __name__ == '__main__':
    predict_stage()
