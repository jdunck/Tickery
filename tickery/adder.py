# Copyright 2010 Fluidinfo Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

import time

from operator import attrgetter

from twisted.internet import defer
from twisted.python import log

from txrdq.rdq import ResizableDispatchQueue
from tickery.cacheutils import DumpingCache
from tickery import ftwitter


class User(object):

    _states = ('queued', 'underway', 'added', 'canceled', 'failed')

    _illegalTransitions = {
        'queued'    : ('added', 'failed'),
        'underway'  : ('queued',),
        'added'     : ('queued', 'underway', 'canceled', 'failed'),
        'canceled' : ('underway', 'added', 'failed'),
        'failed'    : ('underway', 'added'),
        }
    
    def __init__(self, screenname, nFriends):
        self.screenname = screenname
        self.nFriends = nFriends
        self.reset()

    def reset(self):
        self.state = 'queued'
        self.queuedAt = time.time()
        self.failureCount = 0

    def setState(self, newState):
        currentState = self.state
        screenname = self.screenname
        if newState in self._states:
            if newState in self._illegalTransitions[currentState]:
                log.msg("Adder logic error? Can't transition %r from state "
                        '%r to %r.' % (screenname, currentState, newState))
            else:
                if newState == currentState:
                    log.msg('Logic error? %r is already in state %r.' %
                            (screenname, newState))
                else:
                    if newState == 'queued':
                        self.queuedAt = time.time()
                    elif newState == 'underway':
                        now = time.time()
                        log.msg('User %r was queued for %.2f seconds.' %
                                (screenname, now - self.queuedAt))
                        self.underwayAt = now
                    elif newState == 'added':
                        log.msg('User %r was underway for %.2f seconds.' %
                                (screenname, time.time() - self.underwayAt))
                    elif newState == 'failed':
                        self.failureCount += 1
                        log.msg('%r failed. Failure count = %d' %
                                (screenname, self.failureCount))

                    log.msg('Adder: %r state change: %r -> %r.' %
                            (screenname, currentState, newState))
                    self.state = newState
        else:
            log.msg('Error: Unknown state %r.' % newState)

    def canceled(self):
        return self.state == 'canceled'
            
    def __str__(self):
        return ('%-16s state=%s nFriends=%d'
                % (self.screenname, self.state, self.nFriends))
        
    def __repr__(self):
        return '<%s screenname=%r state=%r nFriends=%d>' % (
            self.__class__.__name__, self.screenname, self.state, self.nFriends)


class AdderCache(DumpingCache):

    def __init__(self, cache, queueWidth, endpoint):
        super(AdderCache, self).__init__()
        self.cache = cache
        self.queueWidth = queueWidth
        self.endpoint = endpoint

    def load(self, cacheFile):
        self.rdq = ResizableDispatchQueue(self._addUser, width=self.queueWidth)
        self.users = super(AdderCache, self).load(cacheFile)
        if self.users is None:
            self.users = {}
            self.setCache(self.users)
        else:
            added = [u for u in self.users.values() if u.state == 'added']
            notAdded = [u for u in self.users.values() if u.state != 'added']
            log.msg('Loaded adder cache: found %d added, %d unadded users' %
                    (len(added), len(notAdded)))
            if self.cache.restoreAddQueue:
                log.msg('Restoring add queue.')
                # Re-queue, in original queue order, any users that are
                # marked as having been added.
                for user in sorted(notAdded, key=attrgetter('queuedAt')):
                    log.msg('Restoring %r (previous state %r)' %
                            (user.screenname, user.state))
                    user.reset()
                    self.rdq.put(user)
                    self.clean = False
            else:
                log.msg('Not restoring formerly queued names.')
                # Drop users that were not added last time.
                for user in notAdded:
                    log.msg('Dropping user %r (in state %r)' %
                            (user.screenname, user.state))
                    del self.users[user.screenname.lower()]
                    self.clean = False

    def __str__(self):
        s = [ '%d users in adder cache' % len(self.users) ]
        for key in sorted(self.users.keys()):
            s.append(str(self.users[key]))
        return '\n'.join(s)

    def put(self, screenname, nFriends):
        screennameLower = screenname.lower()
        user = self.users.get(screennameLower)
        if user:
            user.nFriends = nFriends
            user.setState('queued')
        else:
            user = User(screenname, nFriends)
            self.users[screennameLower] = user
        log.msg('Adding screenname %r to request queue.' % screenname)
        self.clean = False
        self.rdq.put(user)

    def _addUser(self, user):
        def _added(result):
            user.setState('added')
            self.clean = False
            return result
        def _failed(fail):
            self.clean = False
            if fail.check(ftwitter.Canceled):
                # The state has been changed to canceled below.
                assert user.canceled()
                log.msg('Addition of user %r canceled.' % user.screenname)
            else:
                user.setState('failed')
                log.msg('Failed to add %r: %s' % (user.screenname, fail))
        log.msg('User %r received from request queue.' % user.screenname)
        user.setState('underway')
        d = ftwitter.addUserByScreenname(self.cache, self.endpoint, user)
        d.addCallbacks(_added, _failed)
        d.addErrback(log.err)        
        return d

    def cancel(self, screenname):
        log.msg('Attempting cancel of %r addition.' % screenname)
        try:
            user = self.users[screenname.lower()]
        except KeyError:
            raise Exception('Cannot cancel unknown user %r.' % screenname)
        else:
            if user.state == 'underway':
                for item in self.rdq.underway():
                    if item.job.screenname == screenname:
                        log.msg('Cancelling underway %r addition.' % screenname)
                        item.cancel()
                        user.setState('canceled')
                        break
                else:
                    raise Exception('Could not find %r in underway list.' %
                                    screenname)
            elif user.state == 'queued':
                queued = self.rdq.pending()
                for i, u in enumerate(queued):
                    if u.screenname == screenname:
                        del queued[i]
                        user.setState('canceled')
                        log.msg('Canceled queued %r addition.' % screenname)
                        break
                else:
                    raise Exception('Could not find %r in queued list.' %
                                    screenname)
            else:
                user.setState('canceled')
                
    def added(self, screenname):
        try:
            user = self.users[screenname.lower()]
        except KeyError:
            return False
        else:
            return user.state == 'added'

    def known(self, screenname):
        return screenname.lower() in self.users

    def statusSummary(self, screennames):
        position = {}
        for i, user in enumerate(self.rdq.pending()):
            position[user.screenname.lower()] = i
        log.msg('position dict is %r' % (position,))
        queued = []
        underway = []
        added = []
        canceled = []
        failed = []
        unknown = []
        for screenname in screennames:
            try:
                user = self.users[screenname.lower()]
            except KeyError:
                unknown.append(screenname)
            else:
                log.msg('user: %s' % user)
                state = user.state
                
                if state == 'queued':
                    try:
                        pos = position[screenname.lower()]
                    except KeyError:
                        log.msg('ERROR: User %r has no queue position.' %
                                screenname)
                        pos = -1
                    queued.append([screenname, user.nFriends, pos])
                elif state == 'underway':
                    underway.append(
                        [screenname, user.nFriends,
                         float(user.workDone) / float(user.workToDo)])
                elif state == 'added':
                    added.append(screenname)
                elif state == 'canceled':
                    canceled.append(screenname)
                elif state == 'failed':
                    failed.append(screenname)
                else:
                    log.msg('ERROR: User %r is in an unknown state: %r' %
                            (screenname, state))
                    
        return {
            'queued' : queued,
            'underway' : underway,
            'added' : added, # NB: 'added' is referred to in ftwitter.py
            'failed' : failed,
            'canceled' : canceled,
            'unknown' : unknown,
            }
    
    @defer.inlineCallbacks
    def close(self):
        pending = yield self.rdq.stop()
        if pending:
            log.msg('Pending user additions canceled: %r' %
                    [p.screenname for p in pending])
        super(AdderCache, self).close()
